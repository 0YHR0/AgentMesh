import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.credential_services import CredentialBrokerService
from agentmesh.application.mcp_registry_services import McpRegistryService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.services import TaskApplicationService
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.config import get_settings
from agentmesh.domain.credentials import CredentialLeaseStatus, SecretProvider, SecretPurpose
from agentmesh.domain.identity import Principal, PrincipalContext, PrincipalType, Role
from agentmesh.domain.mcp_registry import McpTransport
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
from agentmesh.domain.tasks import utc_now
from agentmesh.domain.tools import ToolSideEffect
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


class _Provider:
    def resolve(self, reference) -> str:
        return "mcp-postgres-secret-sentinel"


def _principal(principal_id: str, role: Role, tenant_id: str) -> PrincipalContext:
    return PrincipalContext(
        principal_id=principal_id,
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=True,
        authentication_method="integration",
    )


def test_mcp_workload_binding_and_lease_metadata_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"mcp-http-integration-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    policy = PolicyApprovalService(uow_factory=factory, tenant_id=tenant_id, enabled=True)
    registry = McpRegistryService(
        uow_factory=factory,
        tenant_id=tenant_id,
        policy_service=policy,
    )
    broker = CredentialBrokerService(
        uow_factory=factory,
        tenant_id=tenant_id,
        policy_service=policy,
        provider=_Provider(),
        environment="integration",
    )
    admin = _principal("mcp-admin", Role.TENANT_ADMIN, tenant_id)
    workload = Principal.create(
        principal_id=None,
        tenant_id=tenant_id,
        principal_type=PrincipalType.SERVICE,
        display_name="MCP integration gateway",
    )
    try:
        with factory() as uow:
            uow.identity.add_principal(workload)
            uow.commit()
        server = registry.register_server(
            owner_id="platform",
            name=f"remote-mcp-{uuid4().hex[:8]}",
            description="Authenticated read-only integration fixture",
            transport=McpTransport.STREAMABLE_HTTP,
            endpoint_reference="https://mcp.example/mcp",
            authentication_required=True,
            actor=admin.principal_id,
            idempotency_key="server",
        )
        version = registry.add_version(
            server.id,
            semantic_version="1.0.0",
            protocol_version="2025-11-25",
            configuration={"endpoint": "https://mcp.example/mcp"},
            actor=admin.principal_id,
            idempotency_key="version",
        )
        registry.add_tool(
            version.id,
            logical_key="docs.search",
            tool_name="search",
            description="Search documentation",
            side_effect=ToolSideEffect.READ_ONLY,
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            actor=admin.principal_id,
            idempotency_key="tool",
        )
        published = registry.publish_version(version.id, principal=admin, permit_id=None)
        reference = broker.create_secret_reference(
            provider=SecretProvider.ENVIRONMENT,
            external_key="AGENTMESH_INTEGRATION_MCP_TOKEN",
            version_selector=None,
            purpose=SecretPurpose.MCP_HTTP_BEARER,
            allowed_audiences=("https://mcp.example/mcp",),
            principal=admin,
            idempotency_key="reference",
        )
        expires_at = utc_now() + timedelta(hours=1)
        intent = broker.mcp_binding_intent(
            workload_principal_id=workload.id,
            server_version_id=published.id,
            secret_reference_id=reference.id,
            scopes=("tools:call",),
            environment="integration",
            expires_at=expires_at,
        )
        requested = policy.request_action(
            principal=admin,
            action_type=GovernedActionType.MCP_CREDENTIAL_BINDING_CREATE,
            resource_type="mcp_server",
            resource_id=server.id,
            arguments=intent.arguments,
        )
        approved = policy.decide(
            requested.action.approval_id,
            principal=_principal("mcp-approver", Role.APPROVER, tenant_id),
            outcome=ApprovalOutcome.APPROVE,
            reason="Approved MCP integration credential binding",
        )
        binding = broker.create_mcp_binding(
            workload_principal_id=workload.id,
            server_version_id=published.id,
            secret_reference_id=reference.id,
            scopes=("tools:call",),
            environment="integration",
            expires_at=expires_at,
            principal=admin,
            permit_id=approved.action.permit_id,
            idempotency_key="binding",
        )
        AgentRegistryService(uow_factory=factory, tenant_id=tenant_id).ensure_builtin_agent("local")
        task_service = TaskApplicationService(
            factory,
            "local",
            tenant_id,
            feature_gates=FeatureGateSet.from_config("full"),
        )
        task = task_service.create_task("Authenticated MCP invocation").task
        run = task_service.request_run(task.id).runs[0]
        runtime_binding = registry.resolve("docs.search")
        invocation = ToolInvocationService(uow_factory=factory, tenant_id=tenant_id).start(
            task_id=task.id,
            run_id=run.id,
            binding=runtime_binding,
            arguments={"query": "security"},
        )
        grant = broker.acquire_for_mcp(
            workload_principal_id=workload.id,
            server_id=server.id,
            server_version_id=published.id,
            configuration_digest=published.configuration_digest,
            audience=server.endpoint_reference,
            authentication_required=True,
            tool_invocation_id=invocation.id,
            task_id=task.id,
            run_id=run.id,
        )
        assert grant is not None
        settled = broker.settle_mcp_lease(grant.lease.id, used=True)

        with engine.connect() as connection:
            lease_row = connection.execute(
                text(
                    "SELECT status, binding_id, tool_invocation_id "
                    "FROM mcp_credential_leases WHERE id = :lease_id"
                ),
                {"lease_id": settled.id},
            ).one()
            persisted_sentinel_count = connection.execute(
                text(
                    "SELECT count(*) FROM ("
                    "SELECT row_to_json(r)::text value FROM secret_references r "
                    "UNION ALL SELECT row_to_json(b)::text FROM mcp_credential_bindings b "
                    "UNION ALL SELECT row_to_json(l)::text FROM mcp_credential_leases l"
                    ") records WHERE value LIKE '%mcp-postgres-secret-sentinel%'"
                )
            ).scalar_one()
        assert lease_row == (CredentialLeaseStatus.USED.value, binding.id, invocation.id)
        assert persisted_sentinel_count == 0
        assert grant.material.value == "mcp-postgres-secret-sentinel"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM approval_decisions WHERE governed_action_id IN "
                    "(SELECT id FROM governed_actions WHERE tenant_id = :tenant_id)"
                ),
                {"tenant_id": tenant_id},
            )
            for table in (
                "mcp_credential_leases",
                "mcp_credential_bindings",
                "secret_references",
                "governed_actions",
                "outbox_events",
                "tool_invocations",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
            connection.execute(
                text("DELETE FROM idempotency_records WHERE scope LIKE :scope"),
                {"scope": f"%:{tenant_id}%"},
            )
            connection.execute(
                text(
                    "DELETE FROM task_runs WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE tenant_id = :tenant_id)"
                ),
                {"tenant_id": tenant_id},
            )
            connection.execute(
                text(
                    "DELETE FROM agent_versions WHERE definition_id IN "
                    "(SELECT id FROM agent_definitions WHERE tenant_id = :tenant_id)"
                ),
                {"tenant_id": tenant_id},
            )
            for table in (
                "tasks",
                "mcp_tool_capabilities",
                "mcp_server_versions",
                "mcp_servers",
                "agent_definitions",
                "principals",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
        engine.dispose()
