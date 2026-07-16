from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from agentmesh.application.ports import AgentExecutionContext
from agentmesh.application.services import TaskApplicationService
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.domain.errors import (
    FeatureDisabled,
    InvalidToolRequest,
    TaskNotFound,
    ToolInvocationFailed,
    ToolResultTooLarge,
)
from agentmesh.domain.tools import (
    ToolBinding,
    ToolCallRequest,
    ToolCallResult,
    ToolInvocation,
    ToolInvocationStatus,
    ToolSideEffect,
    canonical_json_digest,
)
from agentmesh.features import FeatureGateSet
from agentmesh.integrations.mcp.client import StdioMcpReadOnlyToolGateway
from agentmesh.integrations.mcp.workspace_server import (
    SERVER_NAME,
    TOOL_NAME,
    read_workspace_text,
)
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.mcp_agent import ReadOnlyMcpAgentExecutor
from tests.fakes import InMemoryUnitOfWorkFactory


class FakeToolGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.invocation_ids: list[UUID] = []

    def invoke(
        self,
        *,
        invocation_id: UUID,
        binding: ToolBinding,
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        self.invocation_ids.append(invocation_id)
        if self.error is not None:
            raise self.error
        output = {"structured_content": {"path": arguments["path"], "content": "hello"}}
        return ToolCallResult(
            output=output,
            protocol_version="2025-11-25",
            schema_digest=canonical_json_digest(
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                }
            ),
            result_digest=canonical_json_digest(output),
            result_bytes=64,
        )


def _binding() -> ToolBinding:
    return ToolBinding(
        logical_key="workspace.read_text",
        server_name=SERVER_NAME,
        tool_name=TOOL_NAME,
        side_effect=ToolSideEffect.READ_ONLY,
    )


def test_tool_call_request_requires_an_explicit_bounded_shape() -> None:
    request = ToolCallRequest.from_task_input(
        {"tool_call": {"tool": "workspace.read_text", "arguments": {"path": "README.md"}}}
    )

    assert request is not None
    assert request.tool_key == "workspace.read_text"
    assert request.arguments == {"path": "README.md"}
    assert canonical_json_digest({"b": 2, "a": 1}) == canonical_json_digest({"a": 1, "b": 2})

    with pytest.raises(InvalidToolRequest, match="only tool and arguments"):
        ToolCallRequest.from_task_input(
            {"tool_call": {"tool": "workspace.read_text", "arguments": {}, "secret": "x"}}
        )
    with pytest.raises(InvalidToolRequest, match="JSON object"):
        ToolCallRequest.from_task_input({"tool_call": None})


def test_task_service_rejects_tool_calls_when_gate_is_disabled(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    service = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("minimal"),
    )

    with pytest.raises(FeatureDisabled, match="mcp_read_tools"):
        service.create_task(
            "Read",
            {
                "tool_call": {
                    "tool": "workspace.read_text",
                    "arguments": {"path": "README.md"},
                }
            },
        )


def test_workspace_reader_confines_paths_and_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    document = root / "document.txt"
    document.write_text("hello", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("AGENTMESH_MCP_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("AGENTMESH_MCP_WORKSPACE_MAX_BYTES", "5")

    result = read_workspace_text("document.txt")

    assert result["content"] == "hello"
    assert result["path"] == "document.txt"
    with pytest.raises(ToolError, match="outside"):
        read_workspace_text("../outside.txt")
    document.write_text("too large", encoding="utf-8")
    with pytest.raises(ToolError, match="maximum"):
        read_workspace_text("document.txt")


def test_official_stdio_client_invokes_the_bundled_read_only_server(tmp_path: Path) -> None:
    document = tmp_path / "notes.txt"
    document.write_text("MCP protocol smoke test", encoding="utf-8")
    gateway = StdioMcpReadOnlyToolGateway(
        command=sys.executable,
        arguments=["-m", "agentmesh.integrations.mcp.workspace_server"],
        environment={
            "AGENTMESH_MCP_WORKSPACE_ROOT": str(tmp_path),
            "AGENTMESH_MCP_WORKSPACE_MAX_BYTES": "1024",
        },
        working_directory=tmp_path,
        timeout_seconds=10,
        max_result_bytes=4096,
    )

    result = gateway.invoke(
        invocation_id=UUID("00000000-0000-0000-0000-000000000001"),
        binding=_binding(),
        arguments={"path": "notes.txt"},
    )

    assert result.protocol_version == "2025-11-25"
    assert result.schema_digest.startswith("sha256:")
    assert result.output["structured_content"]["content"] == "MCP protocol smoke test"

    limited_gateway = StdioMcpReadOnlyToolGateway(
        command=sys.executable,
        arguments=["-m", "agentmesh.integrations.mcp.workspace_server"],
        environment={
            "AGENTMESH_MCP_WORKSPACE_ROOT": str(tmp_path),
            "AGENTMESH_MCP_WORKSPACE_MAX_BYTES": "1024",
        },
        working_directory=tmp_path,
        timeout_seconds=10,
        max_result_bytes=32,
    )
    with pytest.raises(ToolResultTooLarge):
        limited_gateway.invoke(
            invocation_id=UUID("00000000-0000-0000-0000-000000000002"),
            binding=_binding(),
            arguments={"path": "notes.txt"},
        )


def test_mcp_executor_persists_safe_audit_and_returns_provenance(
    task_service: TaskApplicationService,
    tool_invocation_service: ToolInvocationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task = task_service.create_task(
        "Read project documentation",
        {
            "tool_call": {
                "tool": "workspace.read_text",
                "arguments": {"path": "README.md"},
            }
        },
    )
    queued = task_service.request_run(task.task.id)
    run = queued.runs[0]
    gateway = FakeToolGateway()
    executor = ReadOnlyMcpAgentExecutor(
        fallback=DeterministicAgentExecutor(),
        feature_gates=FeatureGateSet.from_config("full"),
        binding=_binding(),
        gateway=gateway,
        invocation_service=tool_invocation_service,
    )

    output = executor.execute(
        objective=task.task.objective,
        input=task.task.input,
        context=AgentExecutionContext(
            task_id=task.task.id,
            run_id=run.id,
            thread_id=run.thread_id,
            agent_id=run.agent_id,
            agent_version_id=run.agent_version_id,
            agent_version_digest=run.agent_version_digest,
        ),
    )

    invocation = next(iter(uow_factory.store.tool_invocations.values()))
    assert gateway.invocation_ids == [invocation.id]
    assert invocation.status is ToolInvocationStatus.SUCCEEDED
    assert invocation.arguments_digest.startswith("sha256:")
    assert not hasattr(invocation, "arguments")
    assert output["tool_invocation"]["id"] == str(invocation.id)
    assert output["tool_invocation"]["result"]["structured_content"]["content"] == "hello"

    other_tenant = ToolInvocationService(uow_factory=uow_factory, tenant_id="other-tenant")
    with pytest.raises(TaskNotFound):
        other_tenant.list_for_task(task.task.id)


def test_mcp_executor_records_failure_without_raw_arguments(
    task_service: TaskApplicationService,
    tool_invocation_service: ToolInvocationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    task = task_service.create_task(
        "Fail safely",
        {
            "tool_call": {
                "tool": "workspace.read_text",
                "arguments": {"path": "private.txt"},
            }
        },
    )
    run = task_service.request_run(task.task.id).runs[0]
    executor = ReadOnlyMcpAgentExecutor(
        fallback=DeterministicAgentExecutor(),
        feature_gates=FeatureGateSet.from_config("full"),
        binding=_binding(),
        gateway=FakeToolGateway(error=ToolInvocationFailed("private detail")),
        invocation_service=tool_invocation_service,
    )

    with pytest.raises(ToolInvocationFailed, match="private detail"):
        executor.execute(
            objective=task.task.objective,
            input=task.task.input,
            context=AgentExecutionContext(
                task_id=task.task.id,
                run_id=run.id,
                thread_id=run.thread_id,
                agent_id=run.agent_id,
                agent_version_id=run.agent_version_id,
                agent_version_digest=run.agent_version_digest,
            ),
        )

    invocation: ToolInvocation = next(iter(uow_factory.store.tool_invocations.values()))
    assert invocation.status is ToolInvocationStatus.FAILED
    assert invocation.error == "MCP invocation failed: ToolInvocationFailed"
