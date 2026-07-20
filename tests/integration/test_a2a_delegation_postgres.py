import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.a2a_delegation_services import A2ADelegationService
from agentmesh.application.a2a_registry_services import A2ARegistryService
from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.services import TaskApplicationService
from agentmesh.config import get_settings
from agentmesh.domain.a2a_delegation import RemoteCorrelationStatus
from agentmesh.domain.a2a_registry import A2ATrustTier
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
from agentmesh.domain.tasks import TaskExecutionMode
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


def _principal(principal_id: str, role: Role, tenant_id: str) -> PrincipalContext:
    return PrincipalContext(
        principal_id=principal_id,
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=True,
        authentication_method="integration",
    )


def test_a2a_delegation_task_run_and_correlation_commit_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"a2a-delegation-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    policy = PolicyApprovalService(uow_factory=factory, tenant_id=tenant_id, enabled=True)
    registry = A2ARegistryService(uow_factory=factory, tenant_id=tenant_id)
    requester = _principal("federation-operator", Role.FEDERATION_OPERATOR, tenant_id)
    try:
        peer = registry.register_peer(
            owner_id="platform",
            name=f"peer-{uuid4().hex[:12]}",
            discovery_url="https://peer.example/.well-known/agent-card.json",
            allowed_endpoint_hosts=["peer.example"],
            allowed_bindings=["HTTP+JSON"],
            trust_tier=A2ATrustTier.TRUSTED,
            actor=requester.principal_id,
            idempotency_key="peer",
        )
        registry.import_card(
            peer.id,
            card={
                "name": "Remote Researcher",
                "description": "Integration fixture",
                "supportedInterfaces": [
                    {
                        "url": "https://peer.example/a2a/v1",
                        "protocolBinding": "HTTP+JSON",
                        "protocolVersion": "1.0",
                    }
                ],
                "version": "1.0.0",
                "capabilities": {},
                "defaultInputModes": ["application/json"],
                "defaultOutputModes": ["application/json"],
                "skills": [
                    {
                        "id": "research",
                        "name": "Research",
                        "description": "Collect evidence",
                        "tags": ["research"],
                    }
                ],
            },
            ttl_seconds=3600,
            source_etag=None,
            actor=requester.principal_id,
            idempotency_key="card",
        )
        gates = FeatureGateSet.from_config(
            "minimal",
            "identity_rbac=true,policy_approval=true,a2a_federation=true,a2a_delegation=true",
        )
        task = (
            TaskApplicationService(factory, "local", tenant_id, feature_gates=gates)
            .create_task("Research A2A", execution_mode=TaskExecutionMode.FEDERATED)
            .task
        )
        delegation = A2ADelegationService(
            uow_factory=factory,
            tenant_id=tenant_id,
            policy_service=policy,
            client=ScriptedA2AClient(
                send_responses=[
                    {
                        "message": {
                            "messageId": "reply",
                            "role": "ROLE_AGENT",
                            "parts": [{"text": "done"}],
                        }
                    }
                ]
            ),
        )
        arguments = delegation.intent(task.id, peer.id).arguments
        requested = policy.request_action(
            principal=requester,
            action_type=GovernedActionType.A2A_DELEGATE,
            resource_type="task",
            resource_id=task.id,
            arguments=arguments,
        )
        approved = policy.decide(
            requested.action.approval_id,
            principal=_principal("approver", Role.APPROVER, tenant_id),
            outcome=ApprovalOutcome.APPROVE,
            reason="Integration approval",
        )
        correlation = delegation.delegate(
            task.id,
            peer.id,
            principal=requester,
            permit_id=approved.action.permit_id,
            idempotency_key="delegate",
        )

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT c.status, t.status AS task_status, r.status AS run_status "
                    "FROM a2a_remote_task_correlations c "
                    "JOIN tasks t ON t.id = c.task_id "
                    "JOIN task_runs r ON r.id = c.run_id WHERE c.id = :id"
                ),
                {"id": correlation.id},
            ).one()
        assert correlation.status is RemoteCorrelationStatus.COMPLETED
        assert row == ("COMPLETED", "COMPLETED", "SUCCEEDED")
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
            for table in ("tasks", "a2a_agent_card_snapshots", "a2a_peers"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
        engine.dispose()
