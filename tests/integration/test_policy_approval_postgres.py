import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.config import get_settings
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.policy import ApprovalOutcome, GovernedActionType
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


def test_policy_approval_and_permit_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"policy-integration-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    service = PolicyApprovalService(
        uow_factory=SqlAlchemyUnitOfWorkFactory(session_factory),
        tenant_id=tenant_id,
        enabled=True,
    )
    requester = _principal("publisher", Role.AGENT_PUBLISHER, tenant_id)
    approver = _principal("approver", Role.APPROVER, tenant_id)
    resource_id = uuid4()
    try:
        requested = service.request_action(
            principal=requester,
            action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
            resource_type="agent_version",
            resource_id=resource_id,
            arguments={"verified_capabilities": ["integration"], "make_default": True},
        )
        assert requested.action.approval_id is not None
        approved = service.decide(
            requested.action.approval_id,
            principal=approver,
            outcome=ApprovalOutcome.APPROVE,
            reason="PostgreSQL integration approval",
        )
        assert approved.action.permit_id is not None
        service.consume_permit(
            approved.action.permit_id,
            principal=requester,
            action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
            resource_type="agent_version",
            resource_id=resource_id,
            arguments={"verified_capabilities": ["integration"], "make_default": True},
        )

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT approval_status, consumed_at FROM governed_actions "
                    "WHERE id = :action_id"
                ),
                {"action_id": requested.action.id},
            ).one()
            decision_count = connection.execute(
                text(
                    "SELECT count(*) FROM approval_decisions WHERE governed_action_id = :action_id"
                ),
                {"action_id": requested.action.id},
            ).scalar_one()
        assert row.approval_status == "CONSUMED"
        assert row.consumed_at is not None
        assert decision_count == 1
    finally:
        engine.dispose()
