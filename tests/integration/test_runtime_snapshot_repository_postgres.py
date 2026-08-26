"""PostgreSQL compatibility readers for immutable Runtime snapshots."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.application.runtime_snapshots import (
    RuntimeAssignmentSnapshot,
    RuntimeHandleSnapshot,
    handle_from_snapshot,
    parse_assignment_payload,
)
from agentmesh.config import get_settings
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeIntegrityIncidentStatus,
)
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.models import (
    AgentDefinitionRecord,
    AgentVersionRecord,
    RuntimeAssignmentSnapshotRecord,
    RuntimeExecutionRecord,
    RuntimeHandleSnapshotRecord,
    RuntimeLifecycleOperationRecord,
    TaskRunRecord,
)
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.runtime_sdk.assignment import RuntimeAssignment, RuntimeExecutionHandle
from agentmesh.runtime_sdk.canonical import canonical_digest, canonical_json_bytes
from tests.integration.test_runtime_control_plane_postgres import _fixture

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run PostgreSQL tests",
    ),
]


def _writer_fixture(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        _, template = _fixture(session)
        run = session.get(TaskRunRecord, template.run_id)
        execution = session.get(RuntimeExecutionRecord, template.id)
        assert run is not None and execution is not None
        agent_definition_id = uuid4()
        agent_version_id = uuid4()
        agent_version_digest = "e" * 64
        # The writer service must be tested against the same FK-backed
        # identity chain as production.  A random UUID in task_runs would
        # violate fk_task_runs_agent_version on PostgreSQL.
        session.add(
            AgentDefinitionRecord(
                id=agent_definition_id,
                tenant_id=template.tenant_id,
                owner_id="runtime-snapshot-test",
                name=f"snapshot-writer-{uuid4().hex}",
                description="runtime snapshot writer integration fixture",
                visibility="PRIVATE",
                lifecycle="ACTIVE",
                default_version_id=None,
                tags=[],
                version=1,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.add(
            AgentVersionRecord(
                id=agent_version_id,
                definition_id=agent_definition_id,
                semantic_version="1.0.0",
                status="PUBLISHED",
                content_digest=agent_version_digest,
                role="EXECUTOR",
                instructions="snapshot writer integration fixture",
                declared_capabilities=[],
                verified_capabilities=[],
                input_schema={},
                output_schema={},
                model_policy={},
                tool_profile={},
                knowledge_profile={},
                policy_profile={},
                risk_class="LOW",
                data_classification_ceiling="PUBLIC",
                resource_defaults={},
                runtime_adapter="test",
                artifact_digest=None,
                execution_modes=["managed_async"],
                compatibility={},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                published_at=datetime.now(timezone.utc),
                revoked_at=None,
                revoke_reason=None,
            )
        )
        run.agent_version_id = agent_version_id
        run.agent_version_digest = agent_version_digest
        session.delete(execution)
        session.commit()
        template = replace(
            template,
            assignment_id=uuid4(),
            assignment_digest="c" * 64,
        )
    uow_factory = SqlAlchemyUnitOfWorkFactory(factory)
    service = RuntimeRegistryService(
        uow_factory=uow_factory,
        tenant_id=template.tenant_id,
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
    )
    return service, template, run.task_id, run.agent_version_id, factory


def _writer_assignment(template, task_id, agent_version_id, execution_id):
    return RuntimeAssignment(
        assignment_id=str(template.assignment_id),
        tenant_id=template.tenant_id,
        task_id=str(task_id),
        run_id=str(template.run_id),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(agent_version_id),
        agent_version_digest="e" * 64,
        runtime_version_id=str(template.runtime_version_id),
        runtime_descriptor_digest="b" * 64,
        execution_mode="managed_async",
        run_role="EXECUTOR",
        revision=0,
        objective="immutable writer objective",
        structured_input={"source": "snapshot-test"},
        correlation_ids={"runtime_execution_id": str(execution_id)},
    )


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


def test_runtime_service_writers_are_atomic_and_exactly_replayable() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        service, template, task_id, agent_version_id, factory = _writer_fixture(engine)
        execution_id = uuid4()
        assignment = _writer_assignment(template, task_id, agent_version_id, execution_id)
        prepared = service.prepare_execution_with_assignment_snapshot(
            run_id=template.run_id,
            assignment=assignment,
            execution_id=execution_id,
        )
        snapshot = service.get_assignment_snapshot(execution_id)
        assert prepared.id == execution_id
        assert snapshot is not None
        assert snapshot.assignment_digest == assignment.assignment_digest
        restored_assignment = parse_assignment_payload(snapshot.canonical_payload)
        assert restored_assignment == assignment
        assert canonical_json_bytes(restored_assignment.to_dict()) == canonical_json_bytes(
            assignment.to_dict()
        )

        replay = service.prepare_execution_with_assignment_snapshot(
            run_id=template.run_id,
            assignment=assignment,
            execution_id=execution_id,
        )
        assert replay.id == prepared.id
        with factory() as session:
            assert session.scalar(
                select(func.count(RuntimeAssignmentSnapshotRecord.id)).where(
                    RuntimeAssignmentSnapshotRecord.runtime_execution_id == execution_id
                )
            ) == 1

        changed = replace(assignment, objective="changed", assignment_digest=None)
        with pytest.raises(RuntimeExecutionConflict):
            service.prepare_execution_with_assignment_snapshot(
                run_id=template.run_id,
                assignment=changed,
                execution_id=execution_id,
            )
        wrong_chain = replace(
            assignment,
            runtime_version_id=str(uuid4()),
            assignment_digest=None,
        )
        with pytest.raises(RuntimeExecutionConflict):
            service.prepare_execution_with_assignment_snapshot(
                run_id=template.run_id,
                assignment=wrong_chain,
                execution_id=execution_id,
            )

        now = datetime.now(timezone.utc)
        with factory() as session:
            session.execute(
                update(RuntimeExecutionRecord)
                .where(RuntimeExecutionRecord.id == execution_id)
                .values(phase="DISPATCHING", updated_at=now, version=2)
            )
            session.commit()
        handle = RuntimeExecutionHandle(
            runtime_execution_id=str(execution_id),
            runtime_version_id=assignment.runtime_version_id,
            provider_execution_ref="opaque-writer-ref",
            assignment_id=assignment.assignment_id,
            assignment_digest=assignment.assignment_digest or "",
            created_at=now,
        )
        bound = service.bind_handle_snapshot(handle=handle)
        persisted_handle = service.get_handle_snapshot(execution_id)
        assert persisted_handle == bound
        assert persisted_handle is not None
        assert handle_from_snapshot(persisted_handle) == handle
        assert service.bind_handle_snapshot(handle=handle) == bound
        changed_handle = replace(handle, provider_execution_ref="different-ref")
        with pytest.raises(RuntimeExecutionConflict):
            service.bind_handle_snapshot(handle=changed_handle)
        wrong_handle = replace(handle, runtime_version_id=str(uuid4()))
        with pytest.raises(RuntimeExecutionConflict):
            service.bind_handle_snapshot(handle=wrong_handle)
        with factory() as session:
            assert session.scalar(
                select(func.count(RuntimeHandleSnapshotRecord.id)).where(
                    RuntimeHandleSnapshotRecord.runtime_execution_id == execution_id
                )
            ) == 1
            persisted_execution = session.get(RuntimeExecutionRecord, execution_id)
            assert persisted_execution is not None
            assert persisted_execution.provider_execution_ref == "opaque-writer-ref"
    finally:
        engine.dispose()


def test_postgres_lifecycle_due_claim_has_one_winner_and_recovers_expired_lease() -> None:
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as session:
            _, execution = _fixture(session)
            now = datetime.now(timezone.utc)
            operation_id = f"runtime-cancel:{execution.id}:v1"
            session.add(
                RuntimeLifecycleOperationRecord(
                    id=uuid4(),
                    tenant_id=execution.tenant_id,
                    runtime_execution_id=execution.id,
                    operation_id=operation_id,
                    operation="cancel",
                    intent_digest="c" * 64,
                    status="REQUESTED",
                    deadline=now + timedelta(minutes=5),
                    receipt_summary=None,
                    attempt_count=0,
                    next_attempt_at=now,
                    claim_token=None,
                    claim_acquired_at=None,
                    claim_expires_at=None,
                    last_error_code=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            execution_id = execution.id

        barrier = Barrier(2)

        def claim():
            with SqlAlchemyUnitOfWorkFactory(factory)() as uow:
                barrier.wait()
                value = uow.runtimes.claim_due_lifecycle(
                    tenant_id=execution.tenant_id,
                    now=now,
                    lease=timedelta(seconds=30),
                    execution_id=execution_id,
                    operation_id=operation_id,
                    has_handle=True,
                )
                uow.commit()
                return value

        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: claim(), range(2)))
        winners = [value for value in values if value is not None]
        assert len(winners) == 1
        assert winners[0].attempt_count == 1
        recovered_at = now + timedelta(seconds=31)
        with SqlAlchemyUnitOfWorkFactory(factory)() as uow:
            recovered = uow.runtimes.claim_due_lifecycle(
                tenant_id=execution.tenant_id,
                now=recovered_at,
                lease=timedelta(seconds=30),
                execution_id=execution_id,
                operation_id=operation_id,
                has_handle=True,
            )
            uow.commit()
        assert recovered is not None
        assert recovered.attempt_count == 2
        assert recovered.claim_token != winners[0].claim_token
    finally:
        engine.dispose()
