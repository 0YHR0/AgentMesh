"""PostgreSQL CAS and append-only audit checks for integrity incidents."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentmesh.domain.errors import InvalidTaskTransition, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentAction,
    RuntimeIntegrityIncidentActionType,
    RuntimeIntegrityIncidentStatus,
)
from agentmesh.infrastructure.postgres.models import (
    RuntimeIntegrityIncidentActionRecord,
    RuntimeIntegrityIncidentRecord,
)
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
            for replay in (
                replace(action, id=uuid4()),
                replace(action, reason="changed bytes"),
                replace(action, created_at=now + timedelta(seconds=1)),
            ):
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
            with pytest.raises(InvalidTaskTransition):
                repository.transition_integrity_incident(
                    incident.id,
                    tenant_id=execution.tenant_id,
                    expected_status=RuntimeIntegrityIncidentStatus.ACKNOWLEDGED,
                    target_status=RuntimeIntegrityIncidentStatus.OPEN,
                    now=now,
                )
            with pytest.raises(RuntimeExecutionConflict):
                repository.transition_integrity_incident(
                    incident.id,
                    tenant_id=execution.tenant_id,
                    expected_status=RuntimeIntegrityIncidentStatus.ACKNOWLEDGED,
                    target_status=RuntimeIntegrityIncidentStatus.ESCALATED,
                    now=now - timedelta(seconds=1),
                )

            with pytest.raises(IntegrityError):
                with session.begin_nested():
                    session.add(
                        RuntimeIntegrityIncidentActionRecord(
                            id=uuid4(),
                            tenant_id=execution.tenant_id,
                            incident_id=incident.id,
                            action="ACKNOWLEDGE",
                            from_status="ACKNOWLEDGED",
                            to_status="ESCALATED",
                            actor_principal_id="operator",
                            reason="invalid direct SQL state",
                            request_digest="d" * 64,
                            created_at=now,
                        )
                    )
                    session.flush()
            with pytest.raises(IntegrityError):
                with session.begin_nested():
                    session.add(
                        RuntimeIntegrityIncidentRecord(
                            id=uuid4(),
                            tenant_id=execution.tenant_id,
                            runtime_execution_id=execution.id,
                            accepted_observation_id="same-digest",
                            accepted_observation_digest="e" * 64,
                            accepted_phase="SUCCEEDED",
                            conflicting_observation_id="same-digest-conflict",
                            conflicting_observation_digest="e" * 64,
                            conflicting_phase="FAILED",
                            status="OPEN",
                            reason="invalid equal digests",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    session.flush()
            with pytest.raises(IntegrityError):
                with session.begin_nested():
                    session.add(
                        RuntimeIntegrityIncidentRecord(
                            id=uuid4(),
                            tenant_id=execution.tenant_id,
                            runtime_execution_id=execution.id,
                            accepted_observation_id="backwards-time",
                            accepted_observation_digest="f" * 64,
                            accepted_phase="SUCCEEDED",
                            conflicting_observation_id="backwards-time-conflict",
                            conflicting_observation_digest="1" * 64,
                            conflicting_phase="FAILED",
                            status="OPEN",
                            reason="invalid timestamp order",
                            created_at=now,
                            updated_at=now - timedelta(seconds=1),
                        )
                    )
                    session.flush()
            assert updated.status is RuntimeIntegrityIncidentStatus.ACKNOWLEDGED
            session.rollback()
    finally:
        engine.dispose()
