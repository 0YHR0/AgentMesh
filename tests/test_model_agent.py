from uuid import uuid4

import pytest

from agentmesh.application.ports import AgentExecutionContext
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.domain.credentials import SecretProvider, SecretPurpose, SecretReference
from agentmesh.domain.errors import InvalidToolRequest
from agentmesh.domain.registry import AgentVersion
from agentmesh.domain.tools import ToolBinding, ToolSideEffect
from agentmesh.features import FeatureGateSet
from agentmesh.orchestration import model_agent
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.mcp_agent import (
    GovernedModelToolRuntime,
    ModelToolResult,
    ModelToolSession,
)
from agentmesh.orchestration.model_agent import (
    ModelProviderError,
    OpenAIResponsesAgentExecutor,
    OpenAIResponsesTransport,
    VersionBoundAgentExecutor,
)
from tests.fakes import InMemoryUnitOfWorkFactory


class StubResponsesTransport:
    def __init__(self, response: dict | list[dict]) -> None:
        self.responses = list(response) if isinstance(response, list) else [response]
        self.payloads: list[dict] = []

    def create(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if not self.responses:
            raise AssertionError("No scripted model response remains")
        return self.responses.pop(0)


class StubModelToolRuntime:
    def __init__(self) -> None:
        self.invocations: list[dict] = []

    def open_session(self, _version) -> ModelToolSession:
        return ModelToolSession(
            definitions=(
                {
                    "type": "function",
                    "name": "agentmesh_workspace",
                    "description": "Read workspace text",
                    "parameters": {"type": "object"},
                    "strict": True,
                },
            ),
            names={"agentmesh_workspace": "workspace.read_text"},
            max_calls=1,
        )

    def invoke(self, **values) -> ModelToolResult:
        self.invocations.append(values)
        return ModelToolResult(
            output={"content": "evidence"},
            invocation_id=str(uuid4()),
            tool_key="workspace.read_text",
            server_name="workspace",
            schema_digest="sha256:" + "1" * 64,
        )


class StubHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def _bound_agent(factory: InMemoryUnitOfWorkFactory):
    aggregate = AgentRegistryService(
        uow_factory=factory, tenant_id="tenant-a"
    ).ensure_builtin_agent(
        "research-agent",
        role="Evidence researcher",
        instructions="Find evidence and clearly identify uncertainty.",
        extra_tags=("research",),
    )
    return aggregate.definition, aggregate.versions[0]


def _context(definition, version, usage: list) -> AgentExecutionContext:
    return AgentExecutionContext(
        tenant_id="tenant-a",
        task_id=uuid4(),
        run_id=uuid4(),
        attempt_id=uuid4(),
        trace_id=uuid4().hex,
        thread_id="thread-a",
        agent_id=definition.name,
        agent_version_id=version.id,
        agent_version_digest=version.content_digest,
        usage_reporter=usage.append,
    )


def test_version_bound_openai_executor_uses_registry_instructions_and_reports_usage() -> None:
    factory = InMemoryUnitOfWorkFactory()
    definition, version = _bound_agent(factory)
    transport = StubResponsesTransport(
        {
            "id": "resp_test",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Evidence summary"}],
                }
            ],
            "usage": {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42},
        }
    )
    model = OpenAIResponsesAgentExecutor(
        transport=transport,
        model="gpt-test",
        reasoning_effort="low",
        max_output_tokens=500,
    )
    executor = VersionBoundAgentExecutor(
        uow_factory=factory,
        fallback=DeterministicAgentExecutor(),
        model_executor=model,
    )
    usage = []

    output = executor.execute(
        objective="Research the market",
        input={"region": "global"},
        context=_context(definition, version, usage),
    )

    assert transport.payloads[0]["instructions"] == version.instructions
    assert transport.payloads[0]["model"] == "gpt-test"
    assert transport.payloads[0]["store"] is False
    assert output["summary"] == "Evidence summary"
    assert output["agent"]["role"] == "Evidence researcher"
    assert output["agent"]["kind"] == "openai-responses"
    assert usage[0].provider == "openai"
    assert usage[0].usage_details == {
        "input_tokens": 30,
        "output_tokens": 12,
        "total_tokens": 42,
    }


def test_version_bound_executor_keeps_zero_credential_fallback_role_aware() -> None:
    factory = InMemoryUnitOfWorkFactory()
    definition, version = _bound_agent(factory)
    executor = VersionBoundAgentExecutor(
        uow_factory=factory,
        fallback=DeterministicAgentExecutor(),
    )

    output = executor.execute(
        objective="Research the market",
        input={},
        context=_context(definition, version, []),
    )

    assert output["agent"]["id"] == "research-agent"
    assert output["agent"]["role"] == "Evidence researcher"
    assert output["agent"]["kind"] == "deterministic-demo"


def test_version_bound_executor_rejects_registry_digest_drift() -> None:
    factory = InMemoryUnitOfWorkFactory()
    definition, version = _bound_agent(factory)
    context = _context(definition, version, [])
    context = AgentExecutionContext(
        **{**context.__dict__, "agent_version_digest": "sha256:" + "0" * 64}
    )
    executor = VersionBoundAgentExecutor(
        uow_factory=factory,
        fallback=DeterministicAgentExecutor(),
    )

    with pytest.raises(ModelProviderError, match="no longer matches"):
        executor.execute(objective="Research", input={}, context=context)


def test_openai_executor_rejects_response_without_output_text() -> None:
    factory = InMemoryUnitOfWorkFactory()
    definition, version = _bound_agent(factory)
    model = OpenAIResponsesAgentExecutor(
        transport=StubResponsesTransport({"id": "resp_empty", "output": []}),
        model="gpt-test",
        reasoning_effort="low",
        max_output_tokens=500,
    )

    with pytest.raises(ModelProviderError, match="no output text"):
        model.execute_version(
            version=version,
            objective="Research",
            input={},
            context=_context(definition, version, []),
        )


def test_openai_executor_runs_bounded_tool_loop_and_replays_complete_output() -> None:
    factory = InMemoryUnitOfWorkFactory()
    definition, version = _bound_agent(factory)
    transport = StubResponsesTransport(
        [
            {
                "id": "resp_call",
                "output": [
                    {"type": "reasoning", "id": "reasoning_1", "summary": []},
                    {
                        "type": "function_call",
                        "name": "agentmesh_workspace",
                        "arguments": '{"path":"README.md"}',
                        "call_id": "call_1",
                    },
                ],
                "usage": {"input_tokens": 20, "output_tokens": 5},
            },
            {
                "id": "resp_final",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Grounded result"}],
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 8},
            },
        ]
    )
    tool_runtime = StubModelToolRuntime()
    model = OpenAIResponsesAgentExecutor(
        transport=transport,
        model="gpt-test",
        reasoning_effort="low",
        max_output_tokens=500,
        tool_runtime=tool_runtime,
    )
    usage = []

    output = model.execute_version(
        version=version,
        objective="Read evidence",
        input={},
        context=_context(definition, version, usage),
    )

    assert output["summary"] == "Grounded result"
    assert len(output["execution"]["model_tool_calls"]) == 1
    replay = transport.payloads[1]["input"]
    assert replay[1]["type"] == "reasoning"
    assert replay[2]["type"] == "function_call"
    assert replay[3] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"content": "evidence"}',
    }
    assert len(usage) == 2


def test_version_policy_resolves_tenant_scoped_model_secret_reference() -> None:
    factory = InMemoryUnitOfWorkFactory()
    definition, _ = _bound_agent(factory)
    reference = SecretReference.create(
        tenant_id="tenant-a",
        provider=SecretProvider.ENVIRONMENT,
        external_key="AGENT_TEST_KEY",
        version_selector=None,
        purpose=SecretPurpose.MODEL_PROVIDER_API_KEY,
        allowed_audiences=("https://api.openai.com",),
        created_by="operator",
    )
    factory.store.secret_references[reference.id] = reference
    version = AgentVersion.create_draft(
        definition_id=definition.id,
        semantic_version="0.2.0",
        role="Evidence researcher",
        instructions="Use evidence.",
        declared_capabilities=("general.task",),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        model_policy={
            "provider": "openai",
            "model": "gpt-policy",
            "reasoning_effort": "medium",
            "max_output_tokens": 700,
            "credential_reference_id": str(reference.id),
        },
    )
    version.submit_for_review()
    version.publish(("general.task",))
    factory.store.agent_versions[version.id] = version
    transport = StubResponsesTransport(
        {
            "id": "resp_policy",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Policy result"}],
                }
            ],
        }
    )
    resolved_keys: list[str] = []

    class SecretProviderStub:
        def resolve(self, value) -> str:
            assert value.id == reference.id
            return "resolved-secret"

    def transport_factory(api_key: str):
        resolved_keys.append(api_key)
        return transport

    executor = VersionBoundAgentExecutor(
        uow_factory=factory,
        fallback=DeterministicAgentExecutor(),
        transport_factory=transport_factory,
        secret_provider=SecretProviderStub(),
    )

    output = executor.execute(
        objective="Research",
        input={},
        context=_context(definition, version, []),
    )

    assert resolved_keys == ["resolved-secret"]
    assert transport.payloads[0]["model"] == "gpt-policy"
    assert output["summary"] == "Policy result"


def test_governed_model_tool_session_rejects_write_capability() -> None:
    factory = InMemoryUnitOfWorkFactory()
    _definition, version = _bound_agent(factory)
    version.tool_profile = {"allowed_tools": ["workspace.write"], "max_calls": 1}

    class WriteCatalog:
        def resolve(self, _logical_key: str) -> ToolBinding:
            return ToolBinding(
                logical_key="workspace.write",
                server_name="workspace",
                tool_name="write",
                side_effect=ToolSideEffect.IDEMPOTENT_WRITE,
                description="Write a file",
                input_schema={"type": "object"},
            )

    gates = FeatureGateSet.from_config(
        "minimal",
        "identity_rbac=true,policy_approval=true,mcp_read_tools=true,"
        "governed_mcp=true,model_tool_loop=true",
    )
    runtime = GovernedModelToolRuntime(
        feature_gates=gates,
        gateway=None,
        invocation_service=None,
        catalog=WriteCatalog(),
    )

    with pytest.raises(InvalidToolRequest, match="must be read-only"):
        runtime.open_session(version)


def test_openai_transport_posts_bounded_json_without_exposing_key(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return StubHttpResponse(b'{"id":"resp_test","output":[]}')

    monkeypatch.setattr(model_agent, "urlopen", fake_urlopen)
    transport = OpenAIResponsesTransport(
        api_key="secret-test-key",
        timeout_seconds=17,
        max_request_bytes=1_024,
        max_response_bytes=1_024,
    )

    response = transport.create({"model": "gpt-test", "input": "hello"})

    assert response["id"] == "resp_test"
    assert captured["timeout"] == 17
    assert captured["request"].full_url == "https://api.openai.com/v1/responses"
    assert captured["request"].headers["Authorization"] == "Bearer secret-test-key"


def test_openai_transport_rejects_oversized_requests_and_responses(monkeypatch) -> None:
    request_limited = OpenAIResponsesTransport(
        api_key="secret-test-key",
        timeout_seconds=10,
        max_request_bytes=10,
        max_response_bytes=100,
    )
    with pytest.raises(ModelProviderError, match="request exceeds"):
        request_limited.create({"input": "too large"})

    monkeypatch.setattr(
        model_agent,
        "urlopen",
        lambda *_args, **_kwargs: StubHttpResponse(b"x" * 12),
    )
    response_limited = OpenAIResponsesTransport(
        api_key="secret-test-key",
        timeout_seconds=10,
        max_request_bytes=100,
        max_response_bytes=10,
    )
    with pytest.raises(ModelProviderError, match="response exceeds"):
        response_limited.create({"input": "ok"})
