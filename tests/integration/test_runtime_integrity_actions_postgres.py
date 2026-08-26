"""PostgreSQL CAS and append-only audit checks for integrity incidents."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentAction,
    RuntimeIntegrityIncidentActionType,
    RuntimeIntegrityIncidentStatus,
)
from agentmesh.infrastructure.postgres.models import RuntimeIntegrityIncidentActionRecord
from tests.integration.test_runtime_control_plane_postgres import _fixture

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL tests",
    ),
]


def test_incident_transition_cas_and_action_replay_are_tenant_safe() -> None:
    from sqlalchemy import create_engine

    from agentmesh.config import get_settings

    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            repository, execution = _fixture(session)
            now = datetime.now(timezone.utc)
            incident = RuntimeIntegrityIncident(
                id=uuid4(),
                tenant_id=execution.tenant_id,
                runtime_execution_id=execution.id,
                accepted_observation_id="accepted",
                accepted_observation_digest="a" * 64,
                accepted_phase=RuntimeExecutionPhase.SUCCEEDED,
                conflicting_observation_id="conflicting",
                conflicting_observation_digest="b" * 64,
                conflicting_phase=RuntimeExecutionPhase.FAILED,
                status=RuntimeIntegrityIncidentStatus.OPEN,
                reason="integration conflict",
                created_at=now,
                updated_at=now,
            )
            repository.add_integrity_incident(incident)
            updated = repository.transition_integrity_incident(
                incident.id,
                tenant_id=execution.tenant_id,
                expected_status=RuntimeIntegrityIncidentStatus.OPEN,
                target_status=RuntimeIntegrityIncidentStatus.ACKNOWLEDGED,
                now=now,
            )
            action = RuntimeIntegrityIncidentAction(
                id=uuid4(),
                tenant_id=execution.tenant_id,
                incident_id=incident.id,
                action=RuntimeIntegrityIncidentActionType.ACKNOWLEDGE,
                from_status=RuntimeIntegrityIncidentStatus.OPEN,
                to_status=RuntimeIntegrityIncidentStatus.ACKNOWLEDGED,
                actor_principal_id="operator",
                reason="reviewed",
                request_digest="c" * 64,
                created_at=now,
            )
            assert repository.add_integrity_incident_action(action) == action
            replay = RuntimeIntegrityIncidentAction(
                **{**action.__dict__, "id": uuid4(), "reason": "changed bytes"}
            )
            with pytest.raises(RuntimeExecutionConflict):
                repository.add_integrity_incident_action(replay)
            assert repository.add_integrity_incident_action(action) == action
            assert repository.get_integrity_incident(incident.id, tenant_id="other") is None
            assert repository.list_integrity_incident_actions(
                incident.id, tenant_id=execution.tenant_id, limit=100, offset=0
            ) == [action]
            assert session.scalar(
                select(func.count(RuntimeIntegrityIncidentActionRecord.id)).where(
                    RuntimeIntegrityIncidentActionRecord.incident_id == incident.id
                )
            ) == 1
            with pytest.raises(RuntimeExecutionConflict):
                repository.transition_integrity_incident(
                    incident.id,
                    tenant_id=execution.tenant_id,
                    expected_status=RuntimeIntegrityIncidentStatus.OPEN,
                    target_status=RuntimeIntegrityIncidentStatus.ESCALATED,
                    now=now,
                )
            assert updated.status is RuntimeIntegrityIncidentStatus.ACKNOWLEDGED
            session.rollback()
    finally:
        engine.dispose()
