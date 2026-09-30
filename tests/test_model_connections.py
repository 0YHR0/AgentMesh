import json
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application import model_connection_services
from agentmesh.application.identity_services import IdentityService
from agentmesh.application.model_connection_services import ModelConnectionService
from agentmesh.application.ports import AgentExecutionContext
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.config import Settings
from agentmesh.domain.errors import (
    AgentRegistryConflict,
    InvalidAgentTransition,
    InvalidAgentVersion,
)
from agentmesh.domain.registry import AgentVersionStatus, AgentVisibility
from agentmesh.orchestration import model_agent
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.model_agent import VersionBoundAgentExecutor
from tests.fakes import InMemoryUnitOfWorkFactory


def test_blank_encryption_key_is_treated_as_unconfigured():
    settings = Settings(_env_file=None, model_connection_encryption_key="")
    assert settings.model_connection_encryption_key is None


def test_connection_secret_is_encrypted_write_only_and_disableable():
    factory = InMemoryUnitOfWorkFactory()
    service = ModelConnectionService(
        uow_factory=factory,
        tenant_id="tenant-a",
        encryption_key=Fernet.generate_key().decode(),
    )
    created = service.create(
        {
            "name": "DeepSeek",
            "provider": "deepseek",
            "credential": {"type": "api_key", "value": "ds-secret-value"},
        }
    )

    assert created["provider"] == "deepseek"
    assert created["model"] == "deepseek-flash"
    assert created["endpoint"] == "https://api.deepseek.com/chat/completions"
    assert "ds-secret-value" not in json.dumps(created)
    persisted = factory.store.model_connections[next(iter(factory.store.model_connections))]
    assert persisted.encrypted_secret != b"ds-secret-value"
    assert service.resolve_secret(persisted) == "ds-secret-value"

    disabled = service.disable(persisted.id)
    assert disabled["enabled"] is False
    assert service.get(persisted.id).enabled is False


def test_connection_api_fails_closed_without_identity_and_sanitizes_secret_validation(
    application_container,
):
    secret_marker = "model-key-must-not-echo-" * 250
    with TestClient(create_app(application_container)) as client:
        response = client.post(
            "/api/v1/model-connections",
            json={
                "name": "DeepSeek",
                "provider": "deepseek",
                "credential": {"type": "api_key", "value": secret_marker},
            },
        )
        assert response.status_code == 422
        assert secret_marker not in response.text

        denied = client.post(
            "/api/v1/model-connections",
            json={
                "name": "DeepSeek",
                "provider": "deepseek",
                "credential": {"type": "api_key", "value": "valid-format-key"},
            },
        )
        assert denied.status_code == 403


def test_authenticated_api_enforces_permission_and_https_or_loopback(
    application_container, uow_factory: InMemoryUnitOfWorkFactory
):
    import hashlib

    admin_token = "admin-model-token-with-thirty-two-plus-characters"
    auditor_token = "auditor-model-token-with-thirty-two-plus-characters"
    application_container.identity_service = IdentityService(
        enabled=True,
        tenant_id="test-tenant",
        principals_json=json.dumps(
            [
                {
                    "principal_id": "admin",
                    "tenant_id": "test-tenant",
                    "roles": ["TENANT_ADMIN"],
                    "token_sha256": hashlib.sha256(admin_token.encode()).hexdigest(),
                },
                {
                    "principal_id": "auditor",
                    "tenant_id": "test-tenant",
                    "roles": ["AUDITOR"],
                    "token_sha256": hashlib.sha256(auditor_token.encode()).hexdigest(),
                },
            ]
        ),
    )
    application_container.model_connection_service = ModelConnectionService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        encryption_key=Fernet.generate_key().decode(),
    )
    app = create_app(application_container)
    payload = {
        "name": "DeepSeek API",
        "provider": "deepseek",
        "credential": {"type": "api_key", "value": "one-time-key"},
    }
    with TestClient(app, client=("127.0.0.1", 51000)) as local:
        created = local.post(
            "/api/v1/model-connections",
            json=payload,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert created.status_code == 200
        assert "one-time-key" not in created.text
    with TestClient(app, client=("203.0.113.9", 51000)) as public_http:
        denied = public_http.post(
            "/api/v1/model-connections",
            json=payload,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert denied.status_code == 400
    with TestClient(app, client=("203.0.113.9", 51000), base_url="https://testserver") as tls:
        allowed = tls.post(
            "/api/v1/model-connections",
            json={**payload, "name": "DeepSeek via TLS"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert allowed.status_code == 200
    with TestClient(app) as readonly:
        listed = readonly.get(
            "/api/v1/model-connections",
            headers={"Authorization": f"Bearer {auditor_token}"},
        )
        assert listed.status_code == 200
        assert "one-time-key" not in listed.text
        denied = readonly.post(
            "/api/v1/model-connections",
            json={**payload, "name": "Auditor attempt"},
            headers={"Authorization": f"Bearer {auditor_token}"},
        )
        assert denied.status_code == 403


def test_wrong_encryption_key_and_cross_tenant_reads_fail_safely():
    factory = InMemoryUnitOfWorkFactory()
    key = Fernet.generate_key().decode()
    owner = ModelConnectionService(uow_factory=factory, tenant_id="tenant-a", encryption_key=key)
    connection = owner.create(
        {
            "name": "OpenAI",
            "provider": "openai",
            "credential": {"type": "api_key", "value": "private-secret"},
        }
    )
    connection_id = UUID(connection["id"])
    other_tenant = ModelConnectionService(
        uow_factory=factory, tenant_id="tenant-b", encryption_key=key
    )
    assert other_tenant.get(connection_id) is None
    wrong_key = ModelConnectionService(
        uow_factory=factory,
        tenant_id="tenant-a",
        encryption_key=Fernet.generate_key().decode(),
    )
    with pytest.raises(InvalidAgentVersion, match="cannot be decrypted"):
        wrong_key.resolve_secret(wrong_key.get(connection_id))


def test_published_model_connection_snapshot_survives_edit_and_disable_is_enforced(
    monkeypatch,
):
    factory = InMemoryUnitOfWorkFactory()
    tenant_id = "snapshot-tenant"
    connections = ModelConnectionService(
        uow_factory=factory,
        tenant_id=tenant_id,
        encryption_key=Fernet.generate_key().decode(),
    )
    raw_connection = connections.create(
        {
            "name": "DeepSeek",
            "provider": "deepseek",
            "credential": {"type": "api_key", "value": "initial-key"},
        }
    )
    connection_id = UUID(raw_connection["id"])
    registry = AgentRegistryService(uow_factory=factory, tenant_id=tenant_id)
    registry.create_capability(
        key="model.connection.execute",
        version="1.0.0",
        description="Run model",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        evidence_requirements=["contract-test"],
    )
    definition = registry.create_definition(
        owner_id="test",
        name="model-runner",
        description="test",
        visibility=AgentVisibility.TENANT,
        tags=[],
    )
    version = registry.create_version(
        definition.definition.id,
        semantic_version="1.0.0",
        role="Executor",
        instructions="Respond briefly.",
        declared_capabilities=["model.connection.execute"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        model_policy={
            "provider": "deepseek",
            "model": "deepseek-flash",
            "max_output_tokens": 512,
            "connection_id": str(connection_id),
        },
        execution_modes=["async"],
    )
    registry.submit_version(version.id)
    published = registry.publish_version(
        version.id,
        verified_capabilities=["model.connection.execute"],
        make_default=True,
    )
    original_digest = published.content_digest
    original_snapshot = published.model_policy["connection_snapshot"]
    assert published.status is AgentVersionStatus.PUBLISHED
    assert original_snapshot["endpoint"] == "https://api.deepseek.com/chat/completions"

    with pytest.raises(InvalidAgentTransition):
        registry.publish_version(
            version.id,
            verified_capabilities=["model.connection.execute"],
            make_default=True,
        )
    reread = registry.get_definition(definition.definition.id).versions[0]
    assert reread.content_digest == original_digest
    assert reread.model_policy["connection_snapshot"] == original_snapshot

    connections.update(
        connection_id,
        {
            "model": "deepseek-v4-pro",
            "endpoint": "https://api.deepseek.com/v1/chat/completions",
            "credential": {"type": "api_key", "value": "rotated-key"},
        },
    )
    opener = _FakeOpener(
        {
            "choices": [{"message": {"content": '{"summary":"done"}', "tool_calls": None}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )
    monkeypatch.setattr(
        model_agent, "urlopen", lambda request, timeout: opener.open(request, timeout)
    )
    executor = VersionBoundAgentExecutor(
        uow_factory=factory,
        fallback=DeterministicAgentExecutor(),
        model_connection_service=connections,
        identity_rbac_enabled=True,
    )
    context = AgentExecutionContext(
        task_id=uuid4(),
        run_id=uuid4(),
        thread_id="thread",
        agent_id="agent",
        agent_version_id=version.id,
        agent_version_digest=original_digest,
        tenant_id=tenant_id,
        attempt_id=uuid4(),
        trace_id=uuid4().hex,
        usage_reporter=lambda _record: None,
    )
    output = executor.execute(objective="execute", input={}, context=context)
    request = json.loads(opener.request.data)
    assert opener.request.full_url == original_snapshot["endpoint"]
    assert request["model"] == "deepseek-flash"
    assert opener.request.get_header("Authorization") == "Bearer rotated-key"
    assert output["agent"]["model"] == "deepseek-flash"

    connections.disable(connection_id)
    with pytest.raises(model_agent.ModelProviderError, match="disabled"):
        executor.execute(objective="execute", input={}, context=context)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.deepseek.com/chat/completions",
        "https://example.com/chat/completions",
        "https://127.0.0.1/chat/completions",
        "https://api.deepseek.com:bad/chat/completions",
    ],
)
def test_connection_rejects_nonofficial_or_malformed_endpoint(endpoint: str):
    service = ModelConnectionService(
        uow_factory=InMemoryUnitOfWorkFactory(),
        tenant_id="tenant-a",
        encryption_key=Fernet.generate_key().decode(),
    )
    with pytest.raises(InvalidAgentVersion):
        service.create(
            {
                "name": "DeepSeek",
                "provider": "deepseek",
                "endpoint": endpoint,
                "credential": {"type": "api_key", "value": "secret"},
            }
        )


def test_connection_endpoint_is_normalized_before_storage():
    service = ModelConnectionService(
        uow_factory=InMemoryUnitOfWorkFactory(),
        tenant_id="tenant-a",
        encryption_key=Fernet.generate_key().decode(),
    )
    connection = service.create(
        {
            "name": "DeepSeek",
            "provider": "deepseek",
            "endpoint": " https://api.deepseek.com/chat/completions ",
            "credential": {"type": "api_key", "value": "secret"},
        }
    )
    assert connection["endpoint"] == "https://api.deepseek.com/chat/completions"


def test_model_connection_name_conflict_covers_update():
    service = ModelConnectionService(
        uow_factory=InMemoryUnitOfWorkFactory(),
        tenant_id="tenant-a",
        encryption_key=Fernet.generate_key().decode(),
    )
    first = service.create(
        {
            "name": "first",
            "provider": "openai",
            "credential": {"type": "api_key", "value": "first-key"},
        }
    )
    second = service.create(
        {
            "name": "second",
            "provider": "openai",
            "credential": {"type": "api_key", "value": "second-key"},
        }
    )
    with pytest.raises(AgentRegistryConflict):
        service.update(UUID(second["id"]), {"name": first["name"]})


class _FakeResponse:
    def __init__(self, value):
        self.raw = json.dumps(value).encode()
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.raw[:size]


class _FakeOpener:
    def __init__(self, value):
        self.value = value
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return _FakeResponse(self.value)


@pytest.mark.parametrize(
    ("provider", "response"),
    [
        ("openai", {"id": "resp-test", "status": "incomplete", "output": []}),
        ("deepseek", {"choices": [{"message": {"content": ""}}]}),
    ],
)
def test_explicit_provider_test_is_bounded_and_accepts_valid_incomplete_result(
    provider, response, monkeypatch
):
    factory = InMemoryUnitOfWorkFactory()
    service = ModelConnectionService(
        uow_factory=factory,
        tenant_id="test-tenant",
        encryption_key=Fernet.generate_key().decode(),
    )
    connection = service.create(
        {
            "name": provider,
            "provider": provider,
            "credential": {"type": "api_key", "value": "test-secret"},
        }
    )
    opener = _FakeOpener(response)
    monkeypatch.setattr(model_connection_services, "build_opener", lambda *_: opener)

    result = service.test(UUID(connection["id"]))

    assert result == {"ok": True, "message": "Provider connection succeeded"}
    assert "test-secret" not in json.dumps(result)
    payload = json.loads(opener.request.data)
    assert payload["max_output_tokens" if provider == "openai" else "max_tokens"] == 128
    if provider == "deepseek":
        assert payload["thinking"] == {"type": "disabled"}


def test_deepseek_chat_adapter_groups_parallel_tool_calls_and_normalizes_usage(monkeypatch):
    opener = _FakeOpener(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "call-a", "function": {"name": "tool_a", "arguments": "{}"}},
                            {"id": "call-b", "function": {"name": "tool_b", "arguments": "{}"}},
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        }
    )
    monkeypatch.setattr(
        model_agent, "urlopen", lambda request, timeout: opener.open(request, timeout)
    )
    transport = model_agent.DeepSeekChatCompletionsTransport(
        api_key="not-real",
        endpoint="https://api.deepseek.com/chat/completions",
        timeout_seconds=5,
        max_request_bytes=20_000,
        max_response_bytes=20_000,
    )
    response = transport.create(
        {
            "model": "deepseek-flash",
            "instructions": "Do the work",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call", "call_id": "old-a", "name": "tool_a", "arguments": "{}"},
                {"type": "function_call", "call_id": "old-b", "name": "tool_b", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "old-a", "output": "a"},
                {"type": "function_call_output", "call_id": "old-b", "output": "b"},
            ],
            "tools": [{"type": "function", "name": "tool_a", "parameters": {"type": "object"}}],
            "max_output_tokens": 512,
        }
    )
    sent = json.loads(opener.request.data)
    assert [message["role"] for message in sent["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert len(sent["messages"][2]["tool_calls"]) == 2
    assert sent["thinking"] == {"type": "disabled"}
    assert response["usage"] == {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}
    assert response["finish_reason"] == "tool_calls"
    assert [item["call_id"] for item in response["output"]] == ["call-a", "call-b"]


def test_deepseek_adapter_rejects_malformed_provider_response(monkeypatch):
    opener = _FakeOpener({"choices": [{"message": "invalid"}]})
    monkeypatch.setattr(
        model_agent, "urlopen", lambda request, timeout: opener.open(request, timeout)
    )
    transport = model_agent.DeepSeekChatCompletionsTransport(
        api_key="not-real",
        endpoint="https://api.deepseek.com/chat/completions",
        timeout_seconds=5,
        max_request_bytes=20_000,
        max_response_bytes=20_000,
    )
    with pytest.raises(model_agent.ModelProviderError, match="DeepSeek response is invalid"):
        transport.create({"model": "deepseek-flash", "input": []})
