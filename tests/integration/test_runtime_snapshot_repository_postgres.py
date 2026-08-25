"""PostgreSQL compatibility readers for immutable Runtime snapshots."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from agentmesh.application.runtime_snapshots import (
    RuntimeAssignmentSnapshot,
    RuntimeHandleSnapshot,
)
from agentmesh.config import get_settings
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentStatus,
)
from agentmesh.infrastructure.postgres.models import (
    RuntimeAssignmentSnapshotRecord,
    RuntimeExecutionRecord,
    RuntimeHandleSnapshotRecord,
    TaskRunRecord,
)
from agentmesh.runtime_sdk.assignment import RuntimeAssignment, RuntimeExecutionHandle
from agentmesh.runtime_sdk.canonical import canonical_digest
from tests.integration.test_runtime_control_plane_postgres import _fixture

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL tests",
    ),
]


def test_snapshot_roundtrip_tenant_scope_replay_and_conflict() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            repository, execution = _fixture(session)
            now = datetime.now(timezone.utc)
            run_record = session.get(TaskRunRecord, execution.run_id)
            assert run_record is not None
            assignment_dto = RuntimeAssignment(
                assignment_id=str(execution.assignment_id),
                tenant_id=execution.tenant_id,
                task_id=str(run_record.task_id),
                run_id=str(execution.run_id),
                agent_definition_id=str(uuid4()),
                agent_version_id=str(uuid4()),
                agent_version_digest="a" * 64,
                runtime_version_id=str(execution.runtime_version_id),
                runtime_descriptor_digest="b" * 64,
                execution_mode="managed_async",
                run_role="EXECUTOR",
                revision=0,
                objective="bounded",
                structured_input={"n": 1},
            )
            execution_record = session.get(RuntimeExecutionRecord, execution.id)
            assert execution_record is not None
            execution_record.assignment_digest = assignment_dto.assignment_digest or ""
            session.flush()
            assignment = RuntimeAssignmentSnapshot(
                id=uuid4(),
                tenant_id=execution.tenant_id,
                runtime_execution_id=execution.id,
                contract_name=assignment_dto.schema_name,
                contract_major=assignment_dto.schema_version,
                assignment_id=execution.assignment_id,
                assignment_digest=assignment_dto.assignment_digest or "",
                canonical_payload=assignment_dto.to_dict(),
                created_at=now,
            )
            assert repository.add_assignment_snapshot(assignment) == assignment
            assert repository.get_assignment_snapshot(
                execution.id, tenant_id=execution.tenant_id
            ) == assignment
            assert repository.get_assignment_snapshot(
                execution.id, tenant_id="other-tenant"
            ) is None
            assignment_replay = RuntimeAssignmentSnapshot(
                **{
                    **assignment.__dict__,
                    "id": uuid4(),
                    "created_at": now + timedelta(seconds=1),
                }
            )
            assert repository.add_assignment_snapshot(assignment_replay) == assignment
            assert session.scalar(
                select(func.count(RuntimeAssignmentSnapshotRecord.id)).where(
                    RuntimeAssignmentSnapshotRecord.runtime_execution_id == execution.id
                )
            ) == 1
            wrong_run_assignment = RuntimeAssignment(
                **{
                    **assignment_dto.__dict__,
                    "run_id": str(uuid4()),
                    "assignment_digest": None,
                }
            )
            with pytest.raises(RuntimeExecutionConflict, match="binding conflicts"):
                repository.add_assignment_snapshot(
                    RuntimeAssignmentSnapshot(
                        id=uuid4(),
                        tenant_id=execution.tenant_id,
                        runtime_execution_id=execution.id,
                        contract_name=wrong_run_assignment.schema_name,
                        contract_major=wrong_run_assignment.schema_version,
                        assignment_id=UUID(wrong_run_assignment.assignment_id),
                        assignment_digest=wrong_run_assignment.assignment_digest or "",
                        canonical_payload=wrong_run_assignment.to_dict(),
                        created_at=now + timedelta(seconds=2),
                    )
                )
            changed_assignment_dto = RuntimeAssignment(
                **{**assignment_dto.__dict__, "objective": "different", "assignment_digest": None}
            )
            with pytest.raises(RuntimeExecutionConflict, match="binding conflicts"):
                repository.add_assignment_snapshot(
                    RuntimeAssignmentSnapshot(
                        id=uuid4(),
                        tenant_id=execution.tenant_id,
                        runtime_execution_id=execution.id,
                        contract_name=changed_assignment_dto.schema_name,
                        contract_major=changed_assignment_dto.schema_version,
                        assignment_id=execution.assignment_id,
                        assignment_digest=changed_assignment_dto.assignment_digest or "",
                        canonical_payload=changed_assignment_dto.to_dict(),
                        created_at=now + timedelta(seconds=2),
                    )
                )

            handle_dto = RuntimeExecutionHandle(
                runtime_execution_id=str(execution.id),
                runtime_version_id=str(execution.runtime_version_id),
                provider_execution_ref="opaque-ref",
                assignment_id=str(execution.assignment_id),
                assignment_digest=assignment_dto.assignment_digest or "",
                created_at=now,
            )
            handle = RuntimeHandleSnapshot(
                id=uuid4(),
                tenant_id=execution.tenant_id,
                runtime_execution_id=execution.id,
                handle_digest=canonical_digest(handle_dto.to_dict()),
                canonical_payload=handle_dto.to_dict(),
                created_at=now,
            )
            assert repository.add_handle_snapshot(handle) == handle
            assert repository.get_handle_snapshot(
                execution.id, tenant_id=execution.tenant_id
            ) == handle
            assert repository.get_handle_snapshot(execution.id, tenant_id="other-tenant") is None
            assert session.scalar(
                select(func.count(RuntimeHandleSnapshotRecord.id)).where(
                    RuntimeHandleSnapshotRecord.runtime_execution_id == execution.id
                )
            ) == 1
            handle_replay = RuntimeHandleSnapshot(
                **{
                    **handle.__dict__,
                    "id": uuid4(),
                    "created_at": now + timedelta(seconds=1),
                }
            )
            assert repository.add_handle_snapshot(handle_replay) == handle

            incident = RuntimeIntegrityIncident(
                id=uuid4(),
                tenant_id=execution.tenant_id,
                runtime_execution_id=execution.id,
                accepted_observation_id="accepted",
                accepted_observation_digest="f" * 64,
                accepted_phase=RuntimeExecutionPhase.SUCCEEDED,
                conflicting_observation_id="conflict",
                conflicting_observation_digest="1" * 64,
                conflicting_phase=RuntimeExecutionPhase.FAILED,
                status=RuntimeIntegrityIncidentStatus.OPEN,
                reason="conflict",
                created_at=now,
                updated_at=now,
            )
            assert repository.add_integrity_incident(incident) == incident
            incident_replay = RuntimeIntegrityIncident(
                **{
                    **incident.__dict__,
                    "id": uuid4(),
                    "reason": "same evidence, retried",
                    "created_at": now + timedelta(seconds=1),
                    "updated_at": now + timedelta(seconds=2),
                }
            )
            assert repository.add_integrity_incident(incident_replay) == incident
            with pytest.raises(RuntimeExecutionConflict, match="conflicting evidence"):
                repository.add_integrity_incident(
                    RuntimeIntegrityIncident(
                        **{
                            **incident.__dict__,
                            "id": uuid4(),
                            "conflicting_observation_id": "different-conflict",
                            "created_at": now + timedelta(seconds=3),
                            "updated_at": now + timedelta(seconds=3),
                        }
                    )
                )
            assert repository.get_integrity_incident(
                incident.id, tenant_id=execution.tenant_id
            ) == incident
            assert repository.get_integrity_incident(
                incident.id, tenant_id="other-tenant"
            ) is None
            assert repository.list_integrity_incidents(
                execution.id, tenant_id=execution.tenant_id, limit=200, offset=0
            ) == [incident]
    finally:
        engine.dispose()
