import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.mcp_registry_services import McpRegistryService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.config import get_settings
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.mcp_registry import (
    McpCapabilityDiscovery,
    McpDiscoveredTool,
    McpDiscoveryStatus,
    McpTransport,
)
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
from agentmesh.domain.tools import ToolSideEffect
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def _principal(principal_id: str, role: Role, tenant_id: str) -> PrincipalContext:
    return PrincipalContext(
        principal_id=principal_id,
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=True,
        authentication_method="integration",
    )


def test_governed_mcp_registry_and_policy_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"mcp-registry-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    policy = PolicyApprovalService(
        uow_factory=factory,
        tenant_id=tenant_id,
        enabled=True,
    )
    registry = McpRegistryService(
        uow_factory=factory,
        tenant_id=tenant_id,
        policy_service=policy,
    )
    provider = _principal("provider", Role.TOOL_PROVIDER, tenant_id)
    approver = _principal("approver", Role.APPROVER, tenant_id)
    schema = {
        "type": "object",
        "properties": {"record": {"type": "string"}},
        "required": ["record"],
    }
    try:
        server = registry.register_server(
            owner_id="provider",
            name=f"postgres-mcp-{uuid4().hex}",
            description="PostgreSQL MCP integration",
            transport=McpTransport.MANAGED_STDIO,
            endpoint_reference="managed://postgres-test",
            actor=provider.principal_id,
            idempotency_key="server",
        )
        version = registry.add_version(
            server.id,
            semantic_version="1.0.0",
            protocol_version="2025-11-25",
            configuration={"adapter": "postgres-test"},
            actor=provider.principal_id,
            idempotency_key="version",
        )
        tool = registry.add_tool(
            version.id,
            logical_key=f"integration.write.{uuid4().hex}",
            tool_name="write_record",
            description="Write one integration record",
            side_effect=ToolSideEffect.IDEMPOTENT_WRITE,
            input_schema=schema,
            actor=provider.principal_id,
            idempotency_key="tool",
        )
        intent = policy.request_action(
            principal=provider,
            action_type=GovernedActionType.MCP_SERVER_VERSION_PUBLISH,
            resource_type="mcp_server_version",
            resource_id=version.id,
            arguments=registry.policy_arguments(version, [tool]),
        )
        approved = policy.decide(
            intent.action.approval_id,
            principal=approver,
            outcome=ApprovalOutcome.APPROVE,
            reason="PostgreSQL integration approval",
        )
        registry.publish_version(
            version.id,
            principal=provider,
            permit_id=approved.action.permit_id,
        )
        binding = registry.resolve(tool.logical_key)

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT s.status, v.status AS version_status, t.side_effect "
                    "FROM mcp_servers s "
                    "JOIN mcp_server_versions v ON v.server_id = s.id "
                    "JOIN mcp_tool_capabilities t ON t.server_version_id = v.id "
                    "WHERE v.id = :version_id"
                ),
                {"version_id": version.id},
            ).one()
        assert binding.server_version_id == version.id
        assert binding.schema_digest == tool.schema_digest
        assert row == ("ACTIVE", "PUBLISHED", "IDEMPOTENT_WRITE")
    finally:
        engine.dispose()


def test_mcp_discovery_snapshot_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"mcp-discovery-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    schema = {"type": "object", "additionalProperties": False}

    class Gateway:
        def discover(self, **kwargs):
            return McpCapabilityDiscovery(
                server_name="postgres-discovery",
                protocol_version="2025-11-25",
                tools=(
                    McpDiscoveredTool.create(
                        name="inspect", input_schema=schema, read_only_hint=True
                    ),
                ),
            )

    policy = PolicyApprovalService(uow_factory=factory, tenant_id=tenant_id, enabled=True)
    registry = McpRegistryService(
        uow_factory=factory,
        tenant_id=tenant_id,
        policy_service=policy,
        discovery_gateway=Gateway(),
    )
    provider = _principal("provider", Role.TOOL_PROVIDER, tenant_id)
    try:
        server = registry.register_server(
            owner_id="provider",
            name="postgres-discovery",
            description="Discovery integration",
            transport=McpTransport.STREAMABLE_HTTP,
            endpoint_reference="https://mcp.example/discovery",
            actor=provider.principal_id,
            idempotency_key="discovery-server",
        )
        version = registry.add_version(
            server.id,
            semantic_version="1.0.0",
            protocol_version="2025-11-25",
            configuration={"adapter": "discovery"},
            actor=provider.principal_id,
            idempotency_key="discovery-version",
        )
        tool = registry.add_tool(
            version.id,
            logical_key="integration.discovery.inspect",
            tool_name="inspect",
            description="Inspect",
            side_effect=ToolSideEffect.READ_ONLY,
            input_schema=schema,
            actor=provider.principal_id,
            idempotency_key="discovery-tool",
        )
        registry.publish_version(version.id, principal=provider, permit_id=None)
        snapshot = registry.refresh_discovery(
            version.id,
            principal=provider,
            idempotency_key="discovery-refresh",
        )

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT status, capability_digest, discovered_tools "
                    "FROM mcp_discovery_snapshots WHERE id = :snapshot_id"
                ),
                {"snapshot_id": snapshot.id},
            ).one()
        assert snapshot.status is McpDiscoveryStatus.COMPATIBLE
        assert row[0] == "COMPATIBLE"
        assert row[1] == snapshot.capability_digest
        assert row[2][0]["name"] == "inspect"
        assert registry.resolve(tool.logical_key).server_version_id == version.id
    finally:
        engine.dispose()
