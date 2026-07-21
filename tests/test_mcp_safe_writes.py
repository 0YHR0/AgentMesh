from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.identity_services import IdentityService
from agentmesh.application.mcp_registry_services import McpRegistryService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.ports import AgentExecutionContext
from agentmesh.application.services import TaskApplicationService
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.domain.errors import (
    ExecutionPermitRequired,
    InvalidToolRequest,
    ToolInvocationFailed,
    ToolOutcomeUnknown,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.mcp_registry import McpTransport
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
from agentmesh.domain.resolutions import McpOutcomeDecision, TaskResolutionAction
from agentmesh.domain.tools import (
    ToolAuthorizationStatus,
    ToolCallRequest,
    ToolCallResult,
    ToolInvocationStatus,
    ToolSideEffect,
    canonical_json_digest,
)
from agentmesh.features import FeatureGateSet
from agentmesh.integrations.mcp.client import (
    StreamableHttpMcpReadOnlyToolGateway,
    _call_bound_tool,
)
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.mcp_agent import ReadOnlyMcpAgentExecutor
from tests.fakes import InMemoryUnitOfWorkFactory

REQUESTER_TOKEN = "safe-write-requester-token-000000000000000000000"
APPROVER_TOKEN = "safe-write-approver-token-0000000000000000000000"

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "idempotency_key": {"type": "string"},
        "value": {"type": "string"},
    },
    "required": ["idempotency_key", "value"],
    "additionalProperties": False,
}
WRITE_GATES = FeatureGateSet.from_config(
    "minimal",
    "mcp_read_tools=true,identity_rbac=true,policy_approval=true,"
    "governed_mcp=true,mcp_write_tools=true",
)
RECONCILIATION_GATES = FeatureGateSet.from_config(
    "minimal",
    "mcp_read_tools=true,identity_rbac=true,policy_approval=true,human_resolution=true,"
    "governed_mcp=true,mcp_write_tools=true,outcome_reconciliation=true",
)


def _principal(principal_id: str, role: Role) -> PrincipalContext:
    return PrincipalContext(
        principal_id=principal_id,
        tenant_id="test-tenant",
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=True,
        authentication_method="test",
    )


def _approved_write_catalog(factory: InMemoryUnitOfWorkFactory):
    policy = PolicyApprovalService(
        uow_factory=factory,
        tenant_id="test-tenant",
        enabled=True,
    )
    registry = McpRegistryService(
        uow_factory=factory,
        tenant_id="test-tenant",
        policy_service=policy,
    )
    provider = _principal("provider", Role.TOOL_PROVIDER)
    approver = _principal("approver", Role.APPROVER)
    server = registry.register_server(
        owner_id="provider",
        name="write-server",
        description="Idempotent write test server",
        transport=McpTransport.STREAMABLE_HTTP,
        endpoint_reference="https://write.example/mcp",
        actor="provider",
        idempotency_key="server",
    )
    version = registry.add_version(
        server.id,
        semantic_version="1.0.0",
        protocol_version="2025-11-25",
        configuration={"environment": "test"},
        actor="provider",
        idempotency_key="version",
    )
    tool = registry.add_tool(
        version.id,
        logical_key="records.upsert",
        tool_name="upsert_record",
        description="Upsert one record",
        side_effect=ToolSideEffect.IDEMPOTENT_WRITE,
        input_schema=WRITE_SCHEMA,
        actor="provider",
        idempotency_key="tool",
    )
    publication = policy.request_action(
        principal=provider,
        action_type=GovernedActionType.MCP_SERVER_VERSION_PUBLISH,
        resource_type="mcp_server_version",
        resource_id=version.id,
        arguments=registry.policy_arguments(version, [tool]),
    )
    assert publication.action.approval_id is not None
    approved = policy.decide(
        publication.action.approval_id,
        principal=approver,
        outcome=ApprovalOutcome.APPROVE,
        reason="Write contract reviewed",
    )
    registry.publish_version(
        version.id,
        principal=provider,
        permit_id=approved.action.permit_id,
    )
    return policy, registry, provider, approver


def _authorize(
    policy: PolicyApprovalService,
    registry: McpRegistryService,
    requester: PrincipalContext,
    approver: PrincipalContext,
    arguments: dict[str, Any],
):
    request = ToolCallRequest.from_task_input(
        {"tool_call": {"tool": "records.upsert", "arguments": arguments}}
    )
    assert request is not None
    intent = registry.request_write_action(
        principal=requester,
        request=request,
        idempotency_key=None,
    )
    assert intent.action.approval_id is not None
    approved = policy.decide(
        intent.action.approval_id,
        principal=approver,
        outcome=ApprovalOutcome.APPROVE,
        reason="Exact write reviewed",
    )
    return request, registry.authorize_write_task(
        principal=requester,
        request=request,
        permit_id=approved.action.permit_id,
    )


class _Gateway:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def invoke(self, **kwargs) -> ToolCallResult:
        if self.error is not None:
            raise self.error
        output = {"structured_content": {"updated": True}}
        return ToolCallResult(
            output=output,
            protocol_version="2025-11-25",
            schema_digest=kwargs["binding"].schema_digest,
            result_digest=canonical_json_digest(output),
            result_bytes=32,
        )


def _execute(
    factory: InMemoryUnitOfWorkFactory,
    registry: McpRegistryService,
    request: ToolCallRequest,
    authorization,
    gateway: _Gateway,
) -> None:
    service = TaskApplicationService(
        uow_factory=factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=WRITE_GATES,
    )
    task = service.create_task(
        "Upsert record",
        {"tool_call": {"tool": request.tool_key, "arguments": request.arguments}},
        tool_authorization=authorization,
    )
    run = service.request_run(task.task.id).runs[0]
    executor = ReadOnlyMcpAgentExecutor(
        fallback=DeterministicAgentExecutor(),
        feature_gates=WRITE_GATES,
        binding=registry.resolve(request.tool_key),
        gateway=gateway,
        invocation_service=ToolInvocationService(
            uow_factory=factory, tenant_id="test-tenant"
        ),
        catalog=registry,
    )
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


def test_idempotent_write_requires_exact_permit_and_settles_authorization(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service,
) -> None:
    policy, registry, requester, approver = _approved_write_catalog(uow_factory)
    request, authorization = _authorize(
        policy,
        registry,
        requester,
        approver,
        {"idempotency_key": "op-123", "value": "new"},
    )

    _execute(uow_factory, registry, request, authorization, _Gateway())

    persisted = next(iter(uow_factory.store.tool_execution_authorizations.values()))
    invocation = next(iter(uow_factory.store.tool_invocations.values()))
    assert persisted.status is ToolAuthorizationStatus.SUCCEEDED
    assert persisted.invocation_id == invocation.id
    assert invocation.status is ToolInvocationStatus.SUCCEEDED
    assert persisted.idempotency_key_digest == canonical_json_digest("op-123")


def test_write_permit_cannot_authorize_changed_arguments(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    policy, registry, requester, approver = _approved_write_catalog(uow_factory)
    original = ToolCallRequest.from_task_input(
        {
            "tool_call": {
                "tool": "records.upsert",
                "arguments": {"idempotency_key": "op-123", "value": "one"},
            }
        }
    )
    assert original is not None
    intent = registry.request_write_action(
        principal=requester, request=original, idempotency_key=None
    )
    assert intent.action.approval_id is not None
    approved = policy.decide(
        intent.action.approval_id,
        principal=approver,
        outcome=ApprovalOutcome.APPROVE,
        reason="Original value only",
    )
    changed = ToolCallRequest.from_task_input(
        {
            "tool_call": {
                "tool": "records.upsert",
                "arguments": {"idempotency_key": "op-123", "value": "two"},
            }
        }
    )
    assert changed is not None
    with pytest.raises(ExecutionPermitRequired, match="does not match"):
        registry.authorize_write_task(
            principal=requester,
            request=changed,
            permit_id=approved.action.permit_id,
        )


def test_unknown_write_outcome_is_terminal_and_authorization_cannot_be_reused(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service,
) -> None:
    policy, registry, requester, approver = _approved_write_catalog(uow_factory)
    request, authorization = _authorize(
        policy,
        registry,
        requester,
        approver,
        {"idempotency_key": "op-unknown", "value": "new"},
    )

    with pytest.raises(ToolOutcomeUnknown):
        _execute(
            uow_factory,
            registry,
            request,
            authorization,
            _Gateway(ToolOutcomeUnknown("response lost")),
        )

    persisted = next(iter(uow_factory.store.tool_execution_authorizations.values()))
    invocation = next(iter(uow_factory.store.tool_invocations.values()))
    assert persisted.status is ToolAuthorizationStatus.OUTCOME_UNKNOWN
    assert invocation.status is ToolInvocationStatus.OUTCOME_UNKNOWN


def test_operator_reconciles_unknown_mcp_success_without_replay(
    application_container,
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service,
) -> None:
    policy, registry, requester, approver = _approved_write_catalog(uow_factory)
    request, authorization = _authorize(
        policy,
        registry,
        requester,
        approver,
        {"idempotency_key": "op-reconciled", "value": "new"},
    )
    with pytest.raises(ToolOutcomeUnknown):
        _execute(
            uow_factory,
            registry,
            request,
            authorization,
            _Gateway(ToolOutcomeUnknown("response lost")),
        )
    invocation = next(iter(uow_factory.store.tool_invocations.values()))
    service = ToolInvocationService(uow_factory=uow_factory, tenant_id="test-tenant")
    evidence_digest = "sha256:" + "a" * 64

    container = replace(
        application_container,
        feature_gates=RECONCILIATION_GATES,
        identity_service=IdentityService(
            enabled=True,
            tenant_id="test-tenant",
            principals_json=json.dumps(
                [
                    {
                        "principal_id": "operator",
                        "tenant_id": "test-tenant",
                        "principal_type": "USER",
                        "status": "ACTIVE",
                        "roles": ["OPERATOR"],
                        "token_sha256": sha256(REQUESTER_TOKEN.encode()).hexdigest(),
                    }
                ]
            ),
        ),
        tool_invocation_service=service,
    )
    with TestClient(create_app(container)) as api:
        response = api.post(
            f"/api/v1/mcp/invocations/{invocation.id}/reconcile-outcome",
            headers={
                "Authorization": f"Bearer {REQUESTER_TOKEN}",
                "Idempotency-Key": "reconcile-mcp-1",
            },
            json={
                "decision": "SUCCEEDED",
                "reason": "Verified in the records system",
                "evidence_reference": "ticket://OPS-123",
                "evidence_digest": evidence_digest,
                "result_digest": "sha256:" + "b" * 64,
                "result_bytes": 42,
            },
        )
    assert response.status_code == 200
    assert response.json()["invocation"]["status"] == "SUCCEEDED"
    result_resolution_id = response.json()["resolution"]["id"]
    replay = service.reconcile_outcome(
        invocation.id,
        principal=_principal("operator", Role.OPERATOR),
        decision=McpOutcomeDecision.SUCCEEDED,
        reason="Verified in the records system",
        evidence_reference="ticket://OPS-123",
        evidence_digest=evidence_digest,
        result_digest="sha256:" + "b" * 64,
        result_bytes=42,
        idempotency_key="reconcile-mcp-1",
    )

    assert replay.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert replay.resolution.action is TaskResolutionAction.RECONCILE_MCP_SUCCEEDED
    assert str(replay.resolution.id) == result_resolution_id
    persisted = next(iter(uow_factory.store.tool_execution_authorizations.values()))
    assert persisted.status is ToolAuthorizationStatus.SUCCEEDED
    assert len(uow_factory.store.task_resolutions) == 1
    with pytest.raises(ToolInvocationFailed, match="Cannot reconcile"):
        service.reconcile_outcome(
            invocation.id,
            principal=_principal("operator", Role.OPERATOR),
            decision=McpOutcomeDecision.FAILED,
            reason="Contradictory claim",
            evidence_reference="ticket://OPS-124",
            evidence_digest="sha256:" + "c" * 64,
            error="not applied",
            idempotency_key="reconcile-mcp-2",
        )


def test_write_contract_rejects_missing_idempotency_key(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    _, registry, requester, _ = _approved_write_catalog(uow_factory)
    missing = ToolCallRequest.from_task_input(
        {"tool_call": {"tool": "records.upsert", "arguments": {"value": "x"}}}
    )
    assert missing is not None
    with pytest.raises(InvalidToolRequest, match="idempotency_key"):
        registry.request_write_action(
            principal=requester, request=missing, idempotency_key=None
        )


def test_streamable_http_retries_unknown_idempotent_write_once() -> None:
    gateway = StreamableHttpMcpReadOnlyToolGateway(
        timeout_seconds=1,
        max_result_bytes=1024,
    )
    binding = type("Binding", (), {"side_effect": ToolSideEffect.IDEMPOTENT_WRITE})()
    expected = ToolCallResult(
        output={"ok": True},
        protocol_version="2025-11-25",
        schema_digest="sha256:" + "1" * 64,
        result_digest="sha256:" + "2" * 64,
        result_bytes=8,
    )
    calls = 0

    async def invoke_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ToolOutcomeUnknown("response lost")
        return expected

    gateway._invoke_async = invoke_once  # type: ignore[method-assign]

    result = asyncio.run(gateway._invoke_with_retry_async(binding=binding))

    assert result is expected
    assert calls == 2


def test_live_write_contract_requires_idempotent_hint_and_forwards_stable_key() -> None:
    binding = SimpleNamespace(
        side_effect=ToolSideEffect.IDEMPOTENT_WRITE,
        server_name="write-server",
        tool_name="upsert_record",
        schema_digest=canonical_json_digest(WRITE_SCHEMA),
    )

    class Session:
        def __init__(self, idempotent_hint: bool) -> None:
            self.idempotent_hint = idempotent_hint
            self.meta = None

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="upsert_record",
                        annotations=SimpleNamespace(
                            readOnlyHint=False,
                            idempotentHint=self.idempotent_hint,
                        ),
                        inputSchema=WRITE_SCHEMA,
                    )
                ]
            )

        async def call_tool(self, name, arguments, **kwargs):
            self.meta = kwargs["meta"]
            return SimpleNamespace(
                isError=False,
                content=[],
                structuredContent={"updated": True},
            )

    initialized = SimpleNamespace(
        serverInfo=SimpleNamespace(name="write-server"),
        protocolVersion="2025-11-25",
    )
    session = Session(True)
    result = asyncio.run(
        _call_bound_tool(
            session=session,
            initialized=initialized,
            invocation_id=UUID("00000000-0000-0000-0000-000000000001"),
            binding=binding,
            arguments={"idempotency_key": "stable-1", "value": "new"},
            timeout=timedelta(seconds=1),
            max_result_bytes=1024,
        )
    )
    assert result.output["structured_content"]["updated"] is True
    assert session.meta["io.agentmesh/idempotency-key-digest"] == canonical_json_digest(
        "stable-1"
    )

    with pytest.raises(ToolInvocationFailed, match="idempotentHint"):
        asyncio.run(
            _call_bound_tool(
                session=Session(False),
                initialized=initialized,
                invocation_id=UUID("00000000-0000-0000-0000-000000000002"),
                binding=binding,
                arguments={"idempotency_key": "stable-1", "value": "new"},
                timeout=timedelta(seconds=1),
                max_result_bytes=1024,
            )
        )


def test_recovered_inflight_write_becomes_unknown_without_second_invocation(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service,
) -> None:
    policy, registry, requester, approver = _approved_write_catalog(uow_factory)
    request, authorization = _authorize(
        policy,
        registry,
        requester,
        approver,
        {"idempotency_key": "op-crash", "value": "new"},
    )
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=WRITE_GATES,
    )
    task = tasks.create_task(
        "Crash recovery",
        {"tool_call": {"tool": request.tool_key, "arguments": request.arguments}},
        tool_authorization=authorization,
    )
    run = tasks.request_run(task.task.id).runs[0]
    service = ToolInvocationService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
    )
    binding = registry.resolve(request.tool_key)
    first = service.start(
        task_id=task.task.id,
        run_id=run.id,
        binding=binding,
        arguments=request.arguments,
    )

    with pytest.raises(ToolOutcomeUnknown, match="automatic replay stopped"):
        service.start(
            task_id=task.task.id,
            run_id=run.id,
            binding=binding,
            arguments=request.arguments,
        )

    assert len(uow_factory.store.tool_invocations) == 1
    assert (
        uow_factory.store.tool_invocations[first.id].status
        is ToolInvocationStatus.OUTCOME_UNKNOWN
    )
    persisted = next(iter(uow_factory.store.tool_execution_authorizations.values()))
    assert persisted.status is ToolAuthorizationStatus.OUTCOME_UNKNOWN


def test_safe_write_intent_and_task_admission_api(
    application_container,
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service,
) -> None:
    policy, registry, _, _ = _approved_write_catalog(uow_factory)
    configured = [
        {
            "principal_id": "provider",
            "tenant_id": "test-tenant",
            "principal_type": "USER",
            "status": "ACTIVE",
            "roles": ["TOOL_PROVIDER", "OPERATOR"],
            "token_sha256": sha256(REQUESTER_TOKEN.encode()).hexdigest(),
        },
        {
            "principal_id": "approver",
            "tenant_id": "test-tenant",
            "principal_type": "USER",
            "status": "ACTIVE",
            "roles": ["APPROVER"],
            "token_sha256": sha256(APPROVER_TOKEN.encode()).hexdigest(),
        },
    ]
    tasks = TaskApplicationService(
        uow_factory=uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=WRITE_GATES,
    )
    container = replace(
        application_container,
        feature_gates=WRITE_GATES,
        identity_service=IdentityService(
            enabled=True,
            tenant_id="test-tenant",
            principals_json=json.dumps(configured),
        ),
        policy_service=policy,
        mcp_registry_service=registry,
        task_service=tasks,
    )
    requester_headers = {
        "Authorization": f"Bearer {REQUESTER_TOKEN}",
        "Idempotency-Key": "api-write-intent",
    }
    approver_headers = {"Authorization": f"Bearer {APPROVER_TOKEN}"}
    arguments = {"idempotency_key": "api-op-1", "value": "new"}

    with TestClient(create_app(container)) as client:
        intent = client.post(
            "/api/v1/mcp/tool-execution-intents",
            headers=requester_headers,
            json={"tool_key": "records.upsert", "arguments": arguments},
        )
        assert intent.status_code == 201
        approved = client.post(
            f"/api/v1/approvals/{intent.json()['approval_id']}/approve",
            headers=approver_headers,
            json={"reason": "API write reviewed"},
        )
        assert approved.status_code == 200
        task = client.post(
            "/api/v1/tasks",
            headers={
                "Authorization": f"Bearer {REQUESTER_TOKEN}",
                "Execution-Permit-Id": approved.json()["permit_id"],
            },
            json={
                "objective": "API write",
                "input": {
                    "tool_call": {
                        "tool": "records.upsert",
                        "arguments": arguments,
                    }
                },
            },
        )
        assert task.status_code == 201
        audit = client.get(
            f"/api/v1/tasks/{task.json()['id']}/tool-invocations",
            headers={"Authorization": f"Bearer {REQUESTER_TOKEN}"},
        )
        assert audit.status_code == 200
        assert audit.json()["items"] == []
        assert audit.json()["authorization"]["status"] == "AUTHORIZED"
        assert "arguments" not in audit.json()["authorization"]

    authorization = next(iter(uow_factory.store.tool_execution_authorizations.values()))
    assert str(authorization.task_id) == task.json()["id"]
    assert authorization.status is ToolAuthorizationStatus.AUTHORIZED
