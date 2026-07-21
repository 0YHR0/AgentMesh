import os
from datetime import timedelta
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
from agentmesh.domain.errors import A2ATransportFailure
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
from agentmesh.domain.resolutions import A2AOutcomeDecision
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
        task_service = TaskApplicationService(factory, "local", tenant_id, feature_gates=gates)
        task = task_service.create_task(
            "Research A2A", execution_mode=TaskExecutionMode.FEDERATED
        ).task
        scripted_client = ScriptedA2AClient(
            send_responses=[
                {
                    "task": {
                        "id": "remote-postgres",
                        "status": {"state": "TASK_STATE_WORKING"},
                    }
                }
            ],
            task_responses=[
                {
                    "id": "remote-postgres",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": "done"}]}],
                }
            ],
            cancel_responses=[
                {
                    "id": "remote-postgres",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            ],
        )
        delegation = A2ADelegationService(
            uow_factory=factory,
            tenant_id=tenant_id,
            policy_service=policy,
            client=scripted_client,
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
        assert correlation.status is RemoteCorrelationStatus.WAITING_REMOTE
        correlation = delegation.cancel(
            correlation.id,
            principal=requester,
            idempotency_key="cancel",
            reason="Integration cancellation",
        )
        assert correlation.status is RemoteCorrelationStatus.CANCEL_PENDING
        assert correlation.cancel_request_count == 1
        assert correlation.cancel_request_digest is not None
        now = utc_now()
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE a2a_remote_task_correlations SET next_poll_at = :due WHERE id = :id"),
                {"due": now - timedelta(seconds=1), "id": correlation.id},
            )
        with factory() as first_uow:
            first_claim = first_uow.remote_correlations.claim_due(
                tenant_id=tenant_id,
                now=now,
                owner="postgres-worker-1",
                lease_expires_at=now + timedelta(seconds=60),
                limit=1,
            )
            with factory() as second_uow:
                second_claim = second_uow.remote_correlations.claim_due(
                    tenant_id=tenant_id,
                    now=now,
                    owner="postgres-worker-2",
                    lease_expires_at=now + timedelta(seconds=60),
                    limit=1,
                )
            assert len(first_claim) == 1
            assert second_claim == []

        report = delegation.reconcile_due(worker_id="postgres-worker", limit=1)
        correlation = delegation.get(correlation.id)

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT c.status, t.status AS task_status, r.status AS run_status, "
                    "c.cancel_request_count, c.cancel_request_digest IS NOT NULL "
                    "FROM a2a_remote_task_correlations c "
                    "JOIN tasks t ON t.id = c.task_id "
                    "JOIN task_runs r ON r.id = c.run_id WHERE c.id = :id"
                ),
                {"id": correlation.id},
            ).one()
        assert correlation.status is RemoteCorrelationStatus.COMPLETED
        assert report.claimed == report.terminal == 1
        assert row == ("COMPLETED", "COMPLETED", "SUCCEEDED", 1, True)

        unknown_task = task_service.create_task(
            "Unknown A2A delivery", execution_mode=TaskExecutionMode.FEDERATED
        ).task
        unknown_arguments = delegation.intent(unknown_task.id, peer.id).arguments
        unknown_requested = policy.request_action(
            principal=requester,
            action_type=GovernedActionType.A2A_DELEGATE,
            resource_type="task",
            resource_id=unknown_task.id,
            arguments=unknown_arguments,
        )
        unknown_approved = policy.decide(
            unknown_requested.action.approval_id,
            principal=_principal("approver-2", Role.APPROVER, tenant_id),
            outcome=ApprovalOutcome.APPROVE,
            reason="Integration unknown-outcome approval",
        )
        scripted_client.send_responses.append(
            A2ATransportFailure("response lost", request_may_have_been_sent=True)
        )
        unknown = delegation.delegate(
            unknown_task.id,
            peer.id,
            principal=requester,
            permit_id=unknown_approved.action.permit_id,
            idempotency_key="delegate-unknown",
        )
        reconciled = delegation.reconcile_unknown_outcome(
            unknown.id,
            principal=requester,
            decision=A2AOutcomeDecision.NOT_DELIVERED,
            reason="Peer logs prove non-delivery",
            evidence_reference="ticket://integration-1",
            evidence_digest="sha256:" + "f" * 64,
            idempotency_key="reconcile-unknown",
        )
        with engine.connect() as connection:
            resolution_row = connection.execute(
                text(
                    "SELECT action, details->>'target_type' FROM task_resolutions "
                    "WHERE id = :id"
                ),
                {"id": reconciled.resolution.id},
            ).one()
        assert resolution_row == ("RECONCILE_A2A_NOT_DELIVERED", "A2A_CORRELATION")
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
