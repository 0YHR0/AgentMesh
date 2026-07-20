import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.a2a_delegation_services import A2ADelegationService
from agentmesh.application.a2a_registry_services import A2ARegistryService
from agentmesh.application.credential_services import CredentialBrokerService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.services import TaskApplicationService
from agentmesh.config import get_settings
from agentmesh.domain.a2a_registry import A2ATrustTier
from agentmesh.domain.credentials import SecretProvider, SecretPurpose
from agentmesh.domain.identity import Principal, PrincipalContext, PrincipalType, Role
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
from agentmesh.domain.tasks import TaskExecutionMode, utc_now
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from tests.fakes import ScriptedA2AClient

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


class _Provider:
    def resolve(self, reference) -> str:
        return "postgres-secret-sentinel"


def _principal(principal_id: str, role: Role, tenant_id: str) -> PrincipalContext:
    return PrincipalContext(
        principal_id=principal_id,
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=True,
        authentication_method="integration",
    )


def test_credential_metadata_and_authenticated_a2a_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"credential-integration-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    policy = PolicyApprovalService(uow_factory=factory, tenant_id=tenant_id, enabled=True)
    broker = CredentialBrokerService(
        uow_factory=factory,
        tenant_id=tenant_id,
        policy_service=policy,
        provider=_Provider(),
        environment="integration",
    )
    registry = A2ARegistryService(uow_factory=factory, tenant_id=tenant_id)
    admin = _principal("credential-admin", Role.TENANT_ADMIN, tenant_id)
    workload = Principal.create(
        principal_id=None,
        tenant_id=tenant_id,
        principal_type=PrincipalType.SERVICE,
        display_name="A2A integration gateway",
    )
    try:
        with factory() as uow:
            uow.identity.add_principal(workload)
            uow.commit()
        peer = registry.register_peer(
            owner_id="platform",
            name=f"secured-peer-{uuid4().hex[:8]}",
            discovery_url="https://peer.example/.well-known/agent-card.json",
            allowed_endpoint_hosts=["peer.example"],
            allowed_bindings=["HTTP+JSON"],
            trust_tier=A2ATrustTier.TRUSTED,
            actor=admin.principal_id,
            idempotency_key="peer",
        )
        registry.import_card(
            peer.id,
            card={
                "name": "Secured PostgreSQL Peer",
                "description": "Credential Broker integration fixture",
                "supportedInterfaces": [
                    {
                        "url": "https://peer.example/a2a/v1",
                        "protocolBinding": "HTTP+JSON",
                        "protocolVersion": "1.0",
                    }
                ],
                "version": "1.0.0",
                "capabilities": {},
                "securitySchemes": {
                    "workloadBearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
                },
                "securityRequirements": [{"workloadBearer": ["a2a.tasks"]}],
                "defaultInputModes": ["application/json"],
                "defaultOutputModes": ["application/json"],
                "skills": [
                    {
                        "id": "echo",
                        "name": "Echo",
                        "description": "Echo one message.",
                        "tags": ["echo"],
                    }
                ],
            },
            ttl_seconds=3600,
            source_etag=None,
            actor=admin.principal_id,
            idempotency_key="card",
        )
        reference = broker.create_secret_reference(
            provider=SecretProvider.ENVIRONMENT,
            external_key="AGENTMESH_INTEGRATION_PEER_TOKEN",
            version_selector=None,
            purpose=SecretPurpose.A2A_HTTP_BEARER,
            allowed_audiences=("https://peer.example/a2a/v1",),
            principal=admin,
            idempotency_key="reference",
        )
        binding_expiry = utc_now() + timedelta(hours=1)
        binding_intent = broker.binding_intent(
            workload_principal_id=workload.id,
            peer_id=peer.id,
            secret_reference_id=reference.id,
            environment="integration",
            expires_at=binding_expiry,
        )
        requested_binding = policy.request_action(
            principal=admin,
            action_type=GovernedActionType.CREDENTIAL_BINDING_CREATE,
            resource_type="a2a_peer",
            resource_id=peer.id,
            arguments=binding_intent.arguments,
        )
        approved_binding = policy.decide(
            requested_binding.action.approval_id,
            principal=_principal("binding-approver", Role.APPROVER, tenant_id),
            outcome=ApprovalOutcome.APPROVE,
            reason="Approved integration binding",
        )
        binding = broker.create_binding(
            workload_principal_id=workload.id,
            peer_id=peer.id,
            secret_reference_id=reference.id,
            environment="integration",
            expires_at=binding_expiry,
            principal=admin,
            permit_id=approved_binding.action.permit_id,
            idempotency_key="binding",
        )
        gates = FeatureGateSet.from_config(
            "minimal",
            "identity_rbac=true,persistent_identity=true,policy_approval=true,"
            "a2a_federation=true,a2a_delegation=true,credential_broker=true",
        )
        task = (
            TaskApplicationService(factory, "local", tenant_id, feature_gates=gates)
            .create_task("Authenticated remote work", execution_mode=TaskExecutionMode.FEDERATED)
            .task
        )
        client = ScriptedA2AClient(
            send_responses=[
                {
                    "message": {
                        "messageId": "reply",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "authenticated"}],
                    }
                }
            ]
        )
        delegation = A2ADelegationService(
            uow_factory=factory,
            tenant_id=tenant_id,
            policy_service=policy,
            client=client,
            credential_broker=broker,
            workload_principal_id=workload.id,
        )
        operator = _principal("federation-operator", Role.FEDERATION_OPERATOR, tenant_id)
        delegation_intent = delegation.intent(task.id, peer.id, binding.id)
        requested_delegation = policy.request_action(
            principal=operator,
            action_type=GovernedActionType.A2A_DELEGATE,
            resource_type="task",
            resource_id=task.id,
            arguments=delegation_intent.arguments,
        )
        approved_delegation = policy.decide(
            requested_delegation.action.approval_id,
            principal=_principal("delegation-approver", Role.APPROVER, tenant_id),
            outcome=ApprovalOutcome.APPROVE,
            reason="Approved authenticated delegation",
        )
        correlation = delegation.delegate(
            task.id,
            peer.id,
            principal=operator,
            permit_id=approved_delegation.action.permit_id,
            idempotency_key="delegation",
            credential_binding_id=binding.id,
        )

        with engine.connect() as connection:
            lease_row = connection.execute(
                text(
                    "SELECT status, binding_id, audience FROM credential_leases "
                    "WHERE id = :lease_id"
                ),
                {"lease_id": correlation.last_credential_lease_id},
            ).one()
            column_names = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name IN "
                        "('secret_references','credential_bindings','credential_leases')"
                    )
                )
            }
            persisted_sentinel_count = connection.execute(
                text(
                    "SELECT count(*) FROM ("
                    "SELECT row_to_json(r)::text value FROM secret_references r "
                    "UNION ALL SELECT row_to_json(b)::text FROM credential_bindings b "
                    "UNION ALL SELECT row_to_json(l)::text FROM credential_leases l"
                    ") records WHERE value LIKE '%postgres-secret-sentinel%'"
                )
            ).scalar_one()
        assert lease_row == ("USED", binding.id, "https://peer.example/a2a/v1")
        assert "value" not in column_names
        assert "token" not in column_names
        assert persisted_sentinel_count == 0
        assert client.send_calls[0]["credential"].value == "postgres-secret-sentinel"
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
                "a2a_remote_task_correlations",
                "credential_leases",
                "credential_bindings",
                "secret_references",
                "governed_actions",
                "outbox_events",
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
            for table in (
                "tasks",
                "a2a_agent_card_snapshots",
                "a2a_peers",
                "principals",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
        engine.dispose()
