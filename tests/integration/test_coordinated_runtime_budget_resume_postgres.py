"""PostgreSQL qualification for managed coordinated budget resumption (c2f6e)."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from agentmesh.api.app import create_app
from agentmesh.application.resolution_services import TaskResolutionService
from agentmesh.bootstrap import build_api_container
from agentmesh.config import get_settings
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
    SubtaskCancellationSource,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import RunRole, TaskStatus
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.models import (
    CoordinationRuntimeDrainRecord,
    IdempotencyRecordModel,
    OutboxEventRecord,
    TaskAttemptRecord,
    TaskRecord,
    TaskResolutionRecord,
    TaskRunRecord,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _cleanup as _dispatch_cleanup,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _fixture as _dispatch_fixture,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _prepare,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run budget resume PostgreSQL tests",
    ),
]


def _resolution_service(fixture) -> TaskResolutionService:
    # The dispatch fixture owns a private AgentDefinition.  Qualify it for the
    # real scheduler, while retaining the fixture's pinned version identity.
    with Session(fixture.engine) as session, session.begin():
        definition = session.execute(
            text("SELECT id FROM agent_definitions WHERE id = :id"),
            {"id": fixture.agent_definition_id},
        ).scalar_one()
        session.execute(
            text(
                "UPDATE agent_definitions SET name = 'integration-agent', "
                "default_version_id = :version WHERE id = :id"
            ),
            {"version": fixture.agent_version_id, "id": definition},
        )
        session.execute(
            text(
                "UPDATE agent_versions SET verified_capabilities = :capabilities, "
                "execution_modes = :modes WHERE id = :id"
            ),
            {
                "capabilities": json.dumps(["general.task", "general.supervise"]),
                "modes": json.dumps(["inline", "managed_async", "async"]),
                "id": fixture.agent_version_id,
            },
        )
    return TaskResolutionService(
        uow_factory=fixture.factory,
        tenant_id=fixture.tenant_id,
        executor_agent_id="integration-agent",
        reviewer_agent_id="integration-agent",
        supervisor_agent_id="integration-agent",
        feature_gates=FeatureGateSet.from_config("full"),
    )


def _budget_fixture(engine):
    fixture = _dispatch_fixture(engine)
    fixture.engine = engine
    try:
        prepared = _prepare(fixture)
        assert prepared.execution_id is not None
        with fixture.factory() as uow:
            task = uow.tasks.get(fixture.task.id, for_update=True)
            subtask = uow.subtasks.get(fixture.run.subtask_id, for_update=True)
            run = uow.runs.get(fixture.run.id, for_update=True)
            attempt = uow.attempts.get(fixture.attempt.id, for_update=True)
            execution = uow.runtimes.get_execution(
                prepared.execution_id, tenant_id=fixture.tenant_id, for_update=True
            )
            assert task is not None and subtask is not None and run is not None
            assert attempt is not None and execution is not None
            now = max(task.updated_at, execution.updated_at, attempt.heartbeat_at) + timedelta(
                seconds=2
            )
            execution = execution.abort_before_dispatch(
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
                now=now,
            )
            drain = CoordinationRuntimeDrain.start(
                drain_id=uuid4(),
                tenant_id=fixture.tenant_id,
                task_id=task.id,
                triggering_run_id=run.id,
                target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
                reason="budget.exhausted",
                at=now,
            )
            # ``subtasks.canceled_by_drain_id`` is a real FK.  Persist the
            # drain row before assigning that provenance so SQLAlchemy's
            # normal autoflush cannot issue the UPDATE before the INSERT.
            uow.coordination_runtime_drains.add(drain)
            uow.flush()
            subtask.cancel_by_drain(
                run.id,
                drain.id,
                source=SubtaskCancellationSource.BUDGET_DRAIN,
                at=now,
            )
            run.cancel(at=now)
            attempt.cancel(at=now)
            task.budget = TaskBudget.create(
                max_runs=5,
                max_tokens=10_000,
                token_reservation_per_attempt=1_000,
            )
            task.budget_revision = 1
            task.wait_for_budget(drain.reason, at=now)
            uow.runtimes.save_execution(execution, tenant_id=fixture.tenant_id)
            uow.subtasks.save(subtask)
            uow.runs.save(run)
            uow.attempts.save(attempt)
            uow.tasks.save(task)
            uow.commit()
        return fixture, drain
    except Exception:
        _cleanup(engine, fixture)
        raise


def _candidate_budget_fixture(engine):
    """Build a terminal managed Supervisor candidate waiting on a budget drain."""
    fixture = _dispatch_fixture(engine)
    fixture.engine = engine
    try:
        prepared = _prepare(fixture)
        assert prepared.execution_id is not None
        with fixture.factory() as uow:
            task = uow.tasks.get(fixture.task.id, for_update=True)
            subtask = uow.subtasks.get(fixture.run.subtask_id, for_update=True)
            run = uow.runs.get(fixture.run.id, for_update=True)
            attempt = uow.attempts.get(fixture.attempt.id, for_update=True)
            execution = uow.runtimes.get_execution(
                prepared.execution_id, tenant_id=fixture.tenant_id, for_update=True
            )
            assert task is not None and subtask is not None and run is not None
            assert attempt is not None and execution is not None
            candidate = {"report": "approved"}
            now = max(task.updated_at, execution.updated_at, attempt.heartbeat_at) + timedelta(
                seconds=2
            )
            execution = execution.apply_observation(
                phase=RuntimeExecutionPhase.SUCCEEDED,
                provider_sequence=0,
                now=now,
            )
            run.succeed(candidate, at=now)
            run.role = RunRole.SUPERVISOR
            run.subtask_id = None
            attempt.succeed(at=now)
            task.budget = TaskBudget.create(
                max_runs=5,
                max_tokens=10_000,
                token_reservation_per_attempt=1_000,
            )
            task.budget_revision = 1
            task.wait_for_budget(
                "budget.max_cost",
                candidate_output=candidate,
                at=now,
            )
            drain = CoordinationRuntimeDrain.start(
                drain_id=uuid4(),
                tenant_id=fixture.tenant_id,
                task_id=task.id,
                triggering_run_id=run.id,
                target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
                reason=task.error,
                at=now,
            )
            uow.runtimes.save_execution(execution, tenant_id=fixture.tenant_id)
            uow.runs.save(run)
            uow.attempts.save(attempt)
            # The Supervisor is not attached to a Subtask in the candidate
            # projection.  Clear the Run FK before deleting the fixture row.
            uow.subtasks.delete_ids(task.id, [subtask.id])
            uow.tasks.save(task)
            uow.coordination_runtime_drains.add(drain)
            uow.commit()
        return fixture, drain
    except Exception:
        _cleanup(engine, fixture)
        raise


def _cleanup(engine, fixture) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM idempotency_records WHERE scope LIKE :scope"),
            {"scope": f"task-resolution:%:{fixture.task.id}"},
        )
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = :tenant_id"),
            {"tenant_id": fixture.tenant_id},
        )
        # The shared dispatch fixture predates cancellation provenance and
        # deletes its drain before the Task cascade deletes Subtasks.  Clear
        # this fixture-owned reference first so cleanup remains idempotent
        # after both successful setup and a partially failed assertion.
        connection.execute(
            text(
                "UPDATE subtasks SET cancellation_source = NULL, "
                "canceled_by_drain_id = NULL WHERE task_id = :task_id"
            ),
            {"task_id": fixture.task.id},
        )
    _dispatch_cleanup(engine, fixture)


def _engine():
    return create_engine(os.environ.get("AGENTMESH_DATABASE_URL", get_settings().database_url))


def _request(fixture):
    return {
        "task_id": fixture.task.id,
        "replacement": TaskBudget.create(
            max_runs=8,
            max_tokens=20_000,
            token_reservation_per_attempt=1_000,
        ),
        "actor": "budget-operator",
        "reason": "resume after approved budget increase",
        "idempotency_key": f"budget-resume-{fixture.task.id}",
    }


def test_postgres_managed_coordinated_budget_resume_is_atomic_and_reopens_exact_drain():
    engine = _engine()
    fixture = None
    try:
        fixture, drain = _budget_fixture(engine)
        result = _resolution_service(fixture).increase_budget_and_resume(**_request(fixture))
        assert result.aggregate.task.status is TaskStatus.RUNNING
        assert result.aggregate.task.budget_revision == 2
        assert result.resolution.details["coordination_runtime_drain_id"] == str(drain.id)
        with Session(engine) as session:
            stored_drain = session.get(CoordinationRuntimeDrainRecord, drain.id)
            subtask = session.execute(
                text(
                    "SELECT status, cancellation_source, canceled_by_drain_id, "
                    "current_run_id FROM subtasks WHERE id = :id"
                ),
                {"id": fixture.run.subtask_id},
            ).one()
            runs = session.scalars(
                select(TaskRunRecord).where(TaskRunRecord.task_id == fixture.task.id)
            ).all()
            attempts = session.scalars(
                select(TaskAttemptRecord).where(TaskAttemptRecord.run_id == fixture.run.id)
            ).all()
            resolutions = session.scalars(
                select(TaskResolutionRecord).where(TaskResolutionRecord.task_id == fixture.task.id)
            ).all()
            events = session.scalars(
                select(OutboxEventRecord).where(OutboxEventRecord.tenant_id == fixture.tenant_id)
            ).all()
            idem = session.scalars(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key == _request(fixture)["idempotency_key"]
                )
            ).all()
            assert stored_drain is not None and stored_drain.status == "COMPLETE"
            scheduled_run_id = UUID(result.resolution.details["scheduled_run_ids"][0])
            assert tuple(subtask) == ("READY", None, None, scheduled_run_id)
            assert len(runs) == 2
            assert len(attempts) == 1 and attempts[0].status == "CANCELED"
            assert len(resolutions) == 1
            assert len(events) == 2
            assert len(idem) == 1
            assert result.resolution.details["reopened_subtask_ids"] == [
                str(fixture.run.subtask_id)
            ]
    finally:
        if fixture is not None:
            _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_budget_resume_exact_replay_has_no_new_projection_or_commit():
    engine = _engine()
    fixture = None
    try:
        fixture, _drain = _budget_fixture(engine)
        service = _resolution_service(fixture)
        first = service.increase_budget_and_resume(**_request(fixture))
        with Session(engine) as session:
            before = (
                session.scalar(select(TaskRecord.version).where(TaskRecord.id == fixture.task.id)),
                tuple(
                    session.scalars(
                        select(TaskRunRecord.id)
                        .where(TaskRunRecord.task_id == fixture.task.id)
                        .order_by(TaskRunRecord.id)
                    )
                ),
                tuple(
                    session.scalars(
                        select(TaskResolutionRecord.id)
                        .where(TaskResolutionRecord.task_id == fixture.task.id)
                        .order_by(TaskResolutionRecord.id)
                    )
                ),
                tuple(
                    session.scalars(
                        select(OutboxEventRecord.id)
                        .where(OutboxEventRecord.tenant_id == fixture.tenant_id)
                        .order_by(OutboxEventRecord.id)
                    )
                ),
            )
        replay = service.increase_budget_and_resume(**_request(fixture))
        assert replay.resolution.id == first.resolution.id
        with Session(engine) as session:
            after = (
                session.scalar(select(TaskRecord.version).where(TaskRecord.id == fixture.task.id)),
                tuple(
                    session.scalars(
                        select(TaskRunRecord.id)
                        .where(TaskRunRecord.task_id == fixture.task.id)
                        .order_by(TaskRunRecord.id)
                    )
                ),
                tuple(
                    session.scalars(
                        select(TaskResolutionRecord.id)
                        .where(TaskResolutionRecord.task_id == fixture.task.id)
                        .order_by(TaskResolutionRecord.id)
                    )
                ),
                tuple(
                    session.scalars(
                        select(OutboxEventRecord.id)
                        .where(OutboxEventRecord.tenant_id == fixture.tenant_id)
                        .order_by(OutboxEventRecord.id)
                    )
                ),
            )
        assert after == before
    finally:
        if fixture is not None:
            _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_budget_resume_same_key_concurrency_has_one_schedule_and_resolution():
    engine = _engine()
    fixture = None
    try:
        fixture, _drain = _budget_fixture(engine)
        request = _request(fixture)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_resolution_service(fixture).increase_budget_and_resume, **request)
                for _ in range(2)
            ]
            results = [future.result(timeout=30) for future in futures]
        assert {value.resolution.id for value in results}.__len__() == 1
        with Session(engine) as session:
            assert (
                session.scalar(
                    select(TaskResolutionRecord.id).where(
                        TaskResolutionRecord.task_id == fixture.task.id
                    )
                )
                is not None
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(TaskRunRecord)
                    .where(TaskRunRecord.task_id == fixture.task.id)
                )
                == 2
            )
    finally:
        if fixture is not None:
            _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_budget_resume_writer_failure_rolls_back_every_projection(monkeypatch):
    engine = _engine()
    fixture = None
    try:
        fixture, drain = _budget_fixture(engine)
        from agentmesh.infrastructure.postgres.repositories import SqlAlchemySubtaskRepository

        original_save = SqlAlchemySubtaskRepository.save

        def fail_save(self, _subtask):
            raise RuntimeExecutionConflict("injected budget resume writer failure")

        monkeypatch.setattr(SqlAlchemySubtaskRepository, "save", fail_save)
        with pytest.raises(RuntimeExecutionConflict, match="injected"):
            _resolution_service(fixture).increase_budget_and_resume(**_request(fixture))
        monkeypatch.setattr(SqlAlchemySubtaskRepository, "save", original_save)
        with Session(engine) as session:
            task = session.get(TaskRecord, fixture.task.id)
            stored_drain = session.get(CoordinationRuntimeDrainRecord, drain.id)
            assert task is not None and task.status == "WAITING_APPROVAL"
            assert stored_drain is not None and stored_drain.status == "DRAINING"
            assert (
                session.scalar(
                    select(TaskResolutionRecord.id).where(
                        TaskResolutionRecord.task_id == fixture.task.id
                    )
                )
                is None
            )
            assert (
                session.scalar(
                    select(IdempotencyRecordModel.scope).where(
                        IdempotencyRecordModel.key == _request(fixture)["idempotency_key"]
                    )
                )
                is None
            )
    finally:
        if fixture is not None:
            _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_candidate_budget_resume_accepts_atomically_and_replays_read_only():
    engine = _engine()
    fixture = None
    try:
        fixture, drain = _candidate_budget_fixture(engine)
        request = _request(fixture)
        result = _resolution_service(fixture).increase_budget_and_resume(**request)
        assert result.aggregate.task.status is TaskStatus.COMPLETED
        assert result.aggregate.task.output == {"report": "approved"}
        assert result.resolution.details["supervisor_run_id"] == str(fixture.run.id)
        assert result.resolution.details["scheduled_run_ids"] == []
        assert result.resolution.details["run_requested_event_ids"] == []

        with Session(engine) as session:
            task = session.get(TaskRecord, fixture.task.id)
            stored_drain = session.get(CoordinationRuntimeDrainRecord, drain.id)
            runs = session.scalars(
                select(TaskRunRecord).where(TaskRunRecord.task_id == fixture.task.id)
            ).all()
            resolutions = session.scalars(
                select(TaskResolutionRecord).where(TaskResolutionRecord.task_id == fixture.task.id)
            ).all()
            events = session.scalars(
                select(OutboxEventRecord).where(OutboxEventRecord.tenant_id == fixture.tenant_id)
            ).all()
            idem = session.scalars(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key == request["idempotency_key"]
                )
            ).all()
            assert task is not None
            assert task.status == "COMPLETED"
            assert task.output == {"report": "approved"}
            assert task.candidate_output == {"report": "approved"}
            assert task.budget_revision == 2
            assert stored_drain is not None and stored_drain.status == "COMPLETE"
            assert len(runs) == 1
            assert len(resolutions) == len(events) == len(idem) == 1
            before = (
                task.version,
                task.budget,
                stored_drain.version,
                tuple(value.id for value in runs),
                tuple(value.id for value in resolutions),
                tuple(value.id for value in events),
                tuple(
                    (
                        value.scope,
                        value.key,
                        value.request_hash,
                        value.result,
                        value.created_at,
                        value.expires_at,
                    )
                    for value in idem
                ),
            )

        replay = _resolution_service(fixture).increase_budget_and_resume(**request)
        assert replay.resolution.id == result.resolution.id
        with Session(engine) as session:
            task = session.get(TaskRecord, fixture.task.id)
            stored_drain = session.get(CoordinationRuntimeDrainRecord, drain.id)
            runs = session.scalars(
                select(TaskRunRecord).where(TaskRunRecord.task_id == fixture.task.id)
            ).all()
            resolutions = session.scalars(
                select(TaskResolutionRecord).where(TaskResolutionRecord.task_id == fixture.task.id)
            ).all()
            events = session.scalars(
                select(OutboxEventRecord).where(OutboxEventRecord.tenant_id == fixture.tenant_id)
            ).all()
            idem = session.scalars(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key == request["idempotency_key"]
                )
            ).all()
            assert task is not None and stored_drain is not None
            after = (
                task.version,
                task.budget,
                stored_drain.version,
                tuple(value.id for value in runs),
                tuple(value.id for value in resolutions),
                tuple(value.id for value in events),
                tuple(
                    (
                        value.scope,
                        value.key,
                        value.request_hash,
                        value.result,
                        value.created_at,
                        value.expires_at,
                    )
                    for value in idem
                ),
            )
        assert after == before
    finally:
        if fixture is not None:
            _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_candidate_budget_resume_writer_failure_rolls_back_every_projection(monkeypatch):
    engine = _engine()
    fixture = None
    try:
        fixture, drain = _candidate_budget_fixture(engine)
        from agentmesh.infrastructure.postgres.repositories import (
            SqlAlchemyTaskResolutionRepository,
        )

        def fail_add(self, _resolution):
            raise RuntimeExecutionConflict("injected candidate budget resolution failure")

        monkeypatch.setattr(SqlAlchemyTaskResolutionRepository, "add", fail_add)
        with pytest.raises(RuntimeExecutionConflict, match="injected candidate budget"):
            _resolution_service(fixture).increase_budget_and_resume(**_request(fixture))

        with Session(engine) as session:
            task = session.get(TaskRecord, fixture.task.id)
            stored_drain = session.get(CoordinationRuntimeDrainRecord, drain.id)
            runs = session.scalars(
                select(TaskRunRecord).where(TaskRunRecord.task_id == fixture.task.id)
            ).all()
            resolutions = session.scalars(
                select(TaskResolutionRecord).where(TaskResolutionRecord.task_id == fixture.task.id)
            ).all()
            events = session.scalars(
                select(OutboxEventRecord).where(OutboxEventRecord.tenant_id == fixture.tenant_id)
            ).all()
            idem = session.scalars(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key == _request(fixture)["idempotency_key"]
                )
            ).all()
            assert task is not None and stored_drain is not None
            assert task.status == "WAITING_APPROVAL"
            assert task.budget_revision == 1
            assert task.candidate_output == {"report": "approved"}
            assert stored_drain.status == "DRAINING"
            assert len(runs) == 1
            assert not resolutions and not events and not idem
    finally:
        if fixture is not None:
            _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_managed_coordinated_pause_resume_api_returns_stable_409_without_writes():
    engine = _engine()
    fixture = None
    container = None
    try:
        fixture = _dispatch_fixture(engine)
        settings = get_settings().model_copy(
            update={
                "database_url": os.environ.get(
                    "AGENTMESH_DATABASE_URL", get_settings().database_url
                ),
                "tenant_id": fixture.tenant_id,
                "feature_profile": "full",
            }
        )
        container = build_api_container(settings)
        with Session(engine) as session:
            before_pause = session.execute(
                text("SELECT status, version, current_run_id FROM tasks WHERE id = :id"),
                {"id": fixture.task.id},
            ).one()
        with TestClient(create_app(container)) as client:
            pause = client.post(f"/api/v1/tasks/{fixture.task.id}/pause")
            assert pause.status_code == 409
            assert pause.json()["code"] == "invalid_task_transition"
            assert pause.json()["message"] == "Managed COORDINATED pause is not enabled"
        with Session(engine) as session:
            after_pause = session.execute(
                text("SELECT status, version, current_run_id FROM tasks WHERE id = :id"),
                {"id": fixture.task.id},
            ).one()
        assert tuple(after_pause) == tuple(before_pause)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE tasks SET status = 'PAUSED' WHERE id = :id"),
                {"id": fixture.task.id},
            )
            connection.execute(
                text("UPDATE task_runs SET status = 'PAUSED' WHERE id = :id"),
                {"id": fixture.run.id},
            )
        with Session(engine) as session:
            before_resume = session.execute(
                text("SELECT status, version, current_run_id FROM tasks WHERE id = :id"),
                {"id": fixture.task.id},
            ).one()
        with TestClient(create_app(container)) as client:
            resume = client.post(f"/api/v1/tasks/{fixture.task.id}/resume")
            assert resume.status_code == 409
            assert resume.json()["code"] == "invalid_task_transition"
            assert resume.json()["message"] == "Managed COORDINATED resume is not enabled"
        with Session(engine) as session:
            after_resume = session.execute(
                text("SELECT status, version, current_run_id FROM tasks WHERE id = :id"),
                {"id": fixture.task.id},
            ).one()
        assert tuple(after_resume) == tuple(before_resume)
    finally:
        if container is not None:
            container.close()
        if fixture is not None:
            _cleanup(engine, fixture)
        engine.dispose()
