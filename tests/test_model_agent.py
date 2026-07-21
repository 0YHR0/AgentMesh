from uuid import uuid4

import pytest

from agentmesh.application.ports import AgentExecutionContext
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.orchestration import model_agent
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.model_agent import (
    ModelProviderError,
    OpenAIResponsesAgentExecutor,
    OpenAIResponsesTransport,
    VersionBoundAgentExecutor,
)
from tests.fakes import InMemoryUnitOfWorkFactory


class StubResponsesTransport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.payloads: list[dict] = []

    def create(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return self.response


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
