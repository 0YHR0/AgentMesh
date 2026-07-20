import json
from dataclasses import replace
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.identity_services import IdentityService
from agentmesh.application.mcp_registry_services import McpRegistryService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.ports import AgentExecutionContext
from agentmesh.application.services import TaskApplicationService
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.errors import (
    ExecutionPermitRequired,
    InvalidMcpRegistry,
    McpRegistryConflict,
    ToolInvocationFailed,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.mcp_registry import (
    McpCapabilityDiscovery,
    McpDiscoveredTool,
    McpDiscoveryStatus,
    McpTransport,
)
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
from agentmesh.domain.tools import ToolCallResult, ToolSideEffect, canonical_json_digest
from agentmesh.features import FeatureGateSet
from agentmesh.integrations.mcp.workspace_server import INPUT_SCHEMA, SERVER_NAME, TOOL_NAME
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.mcp_agent import ReadOnlyMcpAgentExecutor
from tests.fakes import InMemoryUnitOfWorkFactory

PROVIDER_TOKEN = "mcp-provider-token-000000000000000000000000000000"
APPROVER_TOKEN = "mcp-approver-token-000000000000000000000000000000"
SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


def _principal(principal_id: str, role: Role) -> PrincipalContext:
    return PrincipalContext(
        principal_id=principal_id,
        tenant_id="test-tenant",
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=True,
        authentication_method="test",
    )


def _services(uow_factory: InMemoryUnitOfWorkFactory):
    policy = PolicyApprovalService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        enabled=True,
    )
    registry = McpRegistryService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        policy_service=policy,
    )
    return policy, registry


def _draft(
    registry: McpRegistryService,
    *,
    side_effect: ToolSideEffect,
    suffix: str,
):
    server = registry.register_server(
        owner_id="provider",
        name=f"server-{suffix}",
        description="Test Server",
        transport=McpTransport.MANAGED_STDIO,
        endpoint_reference=f"managed://{suffix}",
        actor="provider",
        idempotency_key=f"server-{suffix}",
    )
    version = registry.add_version(
        server.id,
        semantic_version="1.0.0",
        protocol_version="2025-11-25",
        configuration={"adapter": suffix},
        actor="provider",
        idempotency_key=f"version-{suffix}",
    )
    tool = registry.add_tool(
        version.id,
        logical_key=f"test.{suffix}",
        tool_name=f"tool_{suffix}",
        description="Test Tool",
        side_effect=side_effect,
        input_schema=SCHEMA,
        actor="provider",
        idempotency_key=f"tool-{suffix}",
    )
    return server, version, tool


def _published_remote(
    uow_factory: InMemoryUnitOfWorkFactory,
    *,
    gateway,
    suffix: str,
):
    policy = PolicyApprovalService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        enabled=True,
    )
    registry = McpRegistryService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        policy_service=policy,
        discovery_gateway=gateway,
        discovery_ttl_seconds=3600,
    )
    server = registry.register_server(
        owner_id="provider",
        name=f"remote-{suffix}",
        description="Remote discovery fixture",
        transport=McpTransport.STREAMABLE_HTTP,
        endpoint_reference=f"https://{suffix}.example/mcp",
        actor="provider",
        idempotency_key=f"remote-server-{suffix}",
    )
    version = registry.add_version(
        server.id,
        semantic_version="1.0.0",
        protocol_version="2025-11-25",
        configuration={"adapter": suffix},
        actor="provider",
        idempotency_key=f"remote-version-{suffix}",
    )
    tool = registry.add_tool(
        version.id,
        logical_key=f"remote.{suffix}",
        tool_name="search",
        description="Search",
        side_effect=ToolSideEffect.READ_ONLY,
        input_schema=SCHEMA,
        actor="provider",
        idempotency_key=f"remote-tool-{suffix}",
    )
    registry.publish_version(
        version.id,
        principal=_principal("provider", Role.TOOL_PROVIDER),
        permit_id=None,
    )
    return registry, server, version, tool


def test_discovery_refresh_records_expansion_without_widening_catalog() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        def discover(self, **kwargs):
            self.calls += 1
            return McpCapabilityDiscovery(
                server_name="remote-expand",
                protocol_version="2025-11-25",
                tools=(
                    McpDiscoveredTool.create(
                        name="search", input_schema=SCHEMA, read_only_hint=True
                    ),
                    McpDiscoveredTool.create(
                        name="unreviewed", input_schema=SCHEMA, read_only_hint=True
                    ),
                ),
            )

    factory = InMemoryUnitOfWorkFactory()
    gateway = Gateway()
    registry, _, version, tool = _published_remote(
        factory, gateway=gateway, suffix="expand"
    )
    provider = _principal("provider", Role.TOOL_PROVIDER)

    first = registry.refresh_discovery(
        version.id, principal=provider, idempotency_key="refresh-expand"
    )
    replay = registry.refresh_discovery(
        version.id, principal=provider, idempotency_key="refresh-expand"
    )

    assert first.status is McpDiscoveryStatus.EXPANDED
    assert replay.id == first.id
    assert gateway.calls == 1
    assert registry.resolve(tool.logical_key).tool_name == "search"
    with pytest.raises(InvalidMcpRegistry, match="not published"):
        registry.resolve("remote.unreviewed")
    factory.store.mcp_discovery_snapshots[first.id] = replace(
        first, expires_at=first.fetched_at
    )
    with pytest.raises(McpRegistryConflict, match="blocked"):
        registry.resolve(tool.logical_key)


def test_incompatible_or_failed_discovery_blocks_catalog_resolution() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.fail = False

        def discover(self, **kwargs):
            if self.fail:
                raise ToolInvocationFailed("network unavailable")
            return McpCapabilityDiscovery(
                server_name="remote-drift",
                protocol_version="2025-11-25",
                tools=(
                    McpDiscoveredTool.create(
                        name="search",
                        input_schema={"type": "object"},
                        read_only_hint=True,
                    ),
                ),
            )

    factory = InMemoryUnitOfWorkFactory()
    gateway = Gateway()
    registry, _, version, tool = _published_remote(factory, gateway=gateway, suffix="drift")
    provider = _principal("provider", Role.TOOL_PROVIDER)

    drift = registry.refresh_discovery(
        version.id, principal=provider, idempotency_key="refresh-drift"
    )
    assert drift.status is McpDiscoveryStatus.INCOMPATIBLE
    with pytest.raises(McpRegistryConflict, match="blocked"):
        registry.resolve(tool.logical_key)

    gateway.fail = True
    failed = registry.refresh_discovery(
        version.id, principal=provider, idempotency_key="refresh-failed"
    )
    assert failed.status is McpDiscoveryStatus.FAILED
    assert failed.error == "discovery_failed:ToolInvocationFailed"
    assert len(registry.list_discovery_snapshots(version.id, limit=10, offset=0)) == 2


def test_authenticated_discovery_fails_closed_before_network() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.called = False

        def discover(self, **kwargs):
            self.called = True
            raise AssertionError("authenticated discovery must not reach the network")

    factory = InMemoryUnitOfWorkFactory()
    gateway = Gateway()
    policy = PolicyApprovalService(
        uow_factory=factory, tenant_id="test-tenant", enabled=True
    )
    registry = McpRegistryService(
        uow_factory=factory,
        tenant_id="test-tenant",
        policy_service=policy,
        discovery_gateway=gateway,
    )
    server = registry.register_server(
        owner_id="provider",
        name="authenticated-discovery",
        description="",
        transport=McpTransport.STREAMABLE_HTTP,
        endpoint_reference="https://authenticated.example/mcp",
        authentication_required=True,
        actor="provider",
        idempotency_key="authenticated-server",
    )
    version = registry.add_version(
        server.id,
        semantic_version="1.0.0",
        protocol_version="2025-11-25",
        configuration={"adapter": "authenticated"},
        actor="provider",
        idempotency_key="authenticated-version",
    )
    registry.add_tool(
        version.id,
        logical_key="authenticated.search",
        tool_name="search",
        description="",
        side_effect=ToolSideEffect.READ_ONLY,
        input_schema=SCHEMA,
        actor="provider",
        idempotency_key="authenticated-tool",
    )
    registry.publish_version(
        version.id,
        principal=_principal("provider", Role.TOOL_PROVIDER),
        permit_id=None,
    )

    with pytest.raises(InvalidMcpRegistry, match="non-Task credential lease"):
        registry.refresh_discovery(
            version.id,
            principal=_principal("provider", Role.TOOL_PROVIDER),
            idempotency_key="authenticated-refresh",
        )
    assert gateway.called is False


def test_read_only_catalog_publication_resolution_and_revocation() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()
    _, registry = _services(uow_factory)
    server, version, tool = _draft(registry, side_effect=ToolSideEffect.READ_ONLY, suffix="read")
    provider = _principal("provider", Role.TOOL_PROVIDER)

    published = registry.publish_version(version.id, principal=provider, permit_id=None)
    binding = registry.resolve(tool.logical_key)
    assert published.status.value == "PUBLISHED"
    assert binding.server_name == server.name
    assert binding.server_version_id == version.id
    assert binding.schema_digest == tool.schema_digest
    assert registry.publish_version(version.id, principal=provider, permit_id=None) == published
    assert (
        registry.add_tool(
            version.id,
            logical_key="test.read",
            tool_name="tool_read",
            description="Test Tool",
            side_effect=ToolSideEffect.READ_ONLY,
            input_schema=SCHEMA,
            actor="provider",
            idempotency_key="tool-read",
        ).id
        == tool.id
    )

    with pytest.raises(McpRegistryConflict, match="immutable"):
        registry.add_tool(
            version.id,
            logical_key="test.late",
            tool_name="late",
            description="Late mutation",
            side_effect=ToolSideEffect.READ_ONLY,
            input_schema=SCHEMA,
            actor="provider",
            idempotency_key="late",
        )
    registry.revoke_version(version.id, reason="Retired", actor="provider")
    with pytest.raises(InvalidMcpRegistry, match="not published"):
        registry.resolve(tool.logical_key)


def test_write_capability_requires_exact_independent_policy_permit() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()
    policy, registry = _services(uow_factory)
    _, version, tool = _draft(
        registry,
        side_effect=ToolSideEffect.NON_IDEMPOTENT_WRITE,
        suffix="write",
    )
    provider = _principal("provider", Role.TOOL_PROVIDER)
    approver = _principal("approver", Role.APPROVER)

    with pytest.raises(ExecutionPermitRequired):
        registry.publish_version(version.id, principal=provider, permit_id=None)
    arguments = registry.policy_arguments(version, [tool])
    intent = policy.request_action(
        principal=provider,
        action_type=GovernedActionType.MCP_SERVER_VERSION_PUBLISH,
        resource_type="mcp_server_version",
        resource_id=version.id,
        arguments=arguments,
    )
    assert intent.action.approval_id is not None
    approved = policy.decide(
        intent.action.approval_id,
        principal=approver,
        outcome=ApprovalOutcome.APPROVE,
        reason="Reviewed non-idempotent side effects",
    )
    assert approved.action.permit_id is not None
    published = registry.publish_version(
        version.id,
        principal=provider,
        permit_id=approved.action.permit_id,
    )
    assert published.status.value == "PUBLISHED"
    assert registry.resolve(tool.logical_key).side_effect is ToolSideEffect.NON_IDEMPOTENT_WRITE


def test_snapshot_change_after_policy_check_fails_closed() -> None:
    uow_factory = InMemoryUnitOfWorkFactory()

    class MutatingPolicy:
        enabled = True

        def consume_permit(self, permit_id, **kwargs):
            registry.add_tool(
                version.id,
                logical_key="test.race.extra",
                tool_name="extra",
                description="Concurrent addition",
                side_effect=ToolSideEffect.READ_ONLY,
                input_schema=SCHEMA,
                actor="other-provider",
                idempotency_key="race-extra",
            )

    registry = McpRegistryService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        policy_service=MutatingPolicy(),
    )
    _, version, _ = _draft(
        registry,
        side_effect=ToolSideEffect.IDEMPOTENT_WRITE,
        suffix="race",
    )
    with pytest.raises(McpRegistryConflict, match="changed during publication"):
        registry.publish_version(
            version.id,
            principal=_principal("provider", Role.TOOL_PROVIDER),
            permit_id=None,
        )


def test_governed_mcp_api_uses_policy_for_write_snapshots(
    application_container: ApplicationContainer,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    configured = [
        {
            "principal_id": "provider",
            "tenant_id": "test-tenant",
            "principal_type": "USER",
            "status": "ACTIVE",
            "roles": ["TOOL_PROVIDER"],
            "token_sha256": sha256(PROVIDER_TOKEN.encode()).hexdigest(),
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
    policy, registry = _services(uow_factory)
    gates = FeatureGateSet.from_config(
        "full",
        "identity_rbac=true,policy_approval=true,governed_mcp=true",
    )
    container = replace(
        application_container,
        feature_gates=gates,
        identity_service=IdentityService(
            enabled=True,
            tenant_id="test-tenant",
            principals_json=json.dumps(configured),
        ),
        policy_service=policy,
        mcp_registry_service=registry,
    )
    provider_headers = {"Authorization": f"Bearer {PROVIDER_TOKEN}"}
    approver_headers = {"Authorization": f"Bearer {APPROVER_TOKEN}"}
    with TestClient(create_app(container)) as client:
        server = client.post(
            "/api/v1/mcp/servers",
            headers={**provider_headers, "Idempotency-Key": "server"},
            json={
                "owner_id": "provider",
                "name": "api-server",
                "transport": "MANAGED_STDIO",
                "endpoint_reference": "managed://api",
            },
        )
        assert server.status_code == 201
        version = client.post(
            f"/api/v1/mcp/servers/{server.json()['id']}/versions",
            headers={**provider_headers, "Idempotency-Key": "version"},
            json={"semantic_version": "1.0.0", "configuration": {"adapter": "api"}},
        )
        tool = client.post(
            f"/api/v1/mcp/server-versions/{version.json()['id']}/tools",
            headers={**provider_headers, "Idempotency-Key": "tool"},
            json={
                "logical_key": "api.write",
                "tool_name": "write",
                "side_effect": "IDEMPOTENT_WRITE",
                "input_schema": SCHEMA,
            },
        )
        assert tool.status_code == 201
        missing = client.post(
            f"/api/v1/mcp/server-versions/{version.json()['id']}/publish",
            headers=provider_headers,
        )
        assert missing.status_code == 403

        arguments = {
            "configuration_digest": version.json()["configuration_digest"],
            "tools": [
                {
                    "logical_key": "api.write",
                    "tool_name": "write",
                    "schema_digest": tool.json()["schema_digest"],
                    "side_effect": "IDEMPOTENT_WRITE",
                }
            ],
        }
        intent = client.post(
            "/api/v1/policy/actions",
            headers=provider_headers,
            json={
                "action_type": "mcp.server-version.publish",
                "resource_type": "mcp_server_version",
                "resource_id": version.json()["id"],
                "arguments": arguments,
            },
        ).json()
        approved = client.post(
            f"/api/v1/approvals/{intent['approval_id']}/approve",
            headers=approver_headers,
            json={"reason": "Write capability reviewed"},
        ).json()
        published = client.post(
            f"/api/v1/mcp/server-versions/{version.json()['id']}/publish",
            headers={**provider_headers, "Execution-Permit-Id": approved["permit_id"]},
        )
        assert published.status_code == 200
        assert published.json()["status"] == "PUBLISHED"
        assert client.get("/api/v1/mcp/servers", headers=approver_headers).status_code == 200


def test_governed_mcp_api_refreshes_and_lists_discovery_snapshots(
    application_container: ApplicationContainer,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    configured = [
        {
            "principal_id": "provider",
            "tenant_id": "test-tenant",
            "principal_type": "USER",
            "status": "ACTIVE",
            "roles": ["TOOL_PROVIDER"],
            "token_sha256": sha256(PROVIDER_TOKEN.encode()).hexdigest(),
        }
    ]

    class Gateway:
        def discover(self, **kwargs):
            return McpCapabilityDiscovery(
                server_name="api-discovery",
                protocol_version="2025-11-25",
                tools=(
                    McpDiscoveredTool.create(
                        name="search", input_schema=SCHEMA, read_only_hint=True
                    ),
                ),
            )

    policy = PolicyApprovalService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        enabled=True,
    )
    registry = McpRegistryService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        policy_service=policy,
        discovery_gateway=Gateway(),
    )
    container = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config(
            "full", "identity_rbac=true,policy_approval=true,governed_mcp=true"
        ),
        identity_service=IdentityService(
            enabled=True,
            tenant_id="test-tenant",
            principals_json=json.dumps(configured),
        ),
        policy_service=policy,
        mcp_registry_service=registry,
    )
    headers = {"Authorization": f"Bearer {PROVIDER_TOKEN}"}
    with TestClient(create_app(container)) as client:
        server = client.post(
            "/api/v1/mcp/servers",
            headers={**headers, "Idempotency-Key": "discovery-server"},
            json={
                "owner_id": "provider",
                "name": "api-discovery",
                "transport": "STREAMABLE_HTTP",
                "endpoint_reference": "https://mcp.example/discovery",
            },
        ).json()
        version = client.post(
            f"/api/v1/mcp/servers/{server['id']}/versions",
            headers={**headers, "Idempotency-Key": "discovery-version"},
            json={"semantic_version": "1.0.0", "configuration": {"adapter": "api"}},
        ).json()
        client.post(
            f"/api/v1/mcp/server-versions/{version['id']}/tools",
            headers={**headers, "Idempotency-Key": "discovery-tool"},
            json={
                "logical_key": "api.search",
                "tool_name": "search",
                "side_effect": "READ_ONLY",
                "input_schema": SCHEMA,
            },
        )
        published = client.post(
            f"/api/v1/mcp/server-versions/{version['id']}/publish",
            headers=headers,
        )
        assert published.status_code == 200
        refreshed = client.post(
            f"/api/v1/mcp/server-versions/{version['id']}/discovery-snapshots",
            headers={**headers, "Idempotency-Key": "discovery-refresh"},
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["status"] == "COMPATIBLE"
        listed = client.get(
            f"/api/v1/mcp/server-versions/{version['id']}/discovery-snapshots",
            headers=headers,
        )
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == refreshed.json()["id"]


class _CatalogGateway:
    def __init__(self) -> None:
        self.binding = None

    def invoke(self, *, invocation_id, task_id, run_id, binding, arguments):
        self.binding = binding
        return ToolCallResult(
            output={"structured_content": {"content": "catalog"}},
            protocol_version="2025-11-25",
            schema_digest=binding.schema_digest,
            result_digest=canonical_json_digest({"content": "catalog"}),
            result_bytes=21,
        )


def test_executor_resolves_workspace_binding_through_governed_catalog(
    task_service: TaskApplicationService,
    tool_invocation_service: ToolInvocationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    _, registry = _services(uow_factory)
    seeded = registry.ensure_builtin_workspace(
        server_name=SERVER_NAME,
        tool_name=TOOL_NAME,
        logical_key="workspace.read_text",
        input_schema=INPUT_SCHEMA,
    )
    task = task_service.create_task(
        "Read via catalog",
        {"tool_call": {"tool": "workspace.read_text", "arguments": {"path": "README.md"}}},
    )
    run = task_service.request_run(task.task.id).runs[0]
    gateway = _CatalogGateway()
    executor = ReadOnlyMcpAgentExecutor(
        fallback=DeterministicAgentExecutor(),
        feature_gates=FeatureGateSet.from_config(
            "full", "identity_rbac=true,policy_approval=true,governed_mcp=true"
        ),
        binding=seeded,
        gateway=gateway,
        invocation_service=tool_invocation_service,
        catalog=registry,
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
    assert gateway.binding.server_version_id == seeded.server_version_id
    assert gateway.binding.schema_digest == seeded.schema_digest
    assert output["tool_invocation"]["result"]["structured_content"]["content"] == "catalog"
