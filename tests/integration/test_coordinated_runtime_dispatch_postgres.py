"""Real PostgreSQL qualification for the c2c2 coordinated prepare command."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedRuntimeDispatchService,
    CoordinatedRuntimePrepareKind,
)
from agentmesh.config import get_settings
from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
    Subtask,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import Task, TaskAttempt, TaskExecutionMode, TaskRun
from agentmesh.infrastructure.postgres.models import (
    AgentDefinitionRecord,
    AgentVersionRecord,
    RuntimeAssignmentSnapshotRecord,
    RuntimeExecutionRecord,
    TaskRunRecord,
)
from agentmesh.infrastructure.postgres.repositories import (
    SqlAlchemySubtaskRepository,
    SqlAlchemyTaskAttemptRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskRunRepository,
)
from agentmesh.infrastructure.postgres.runtime_repositories import SqlAlchemyRuntimeRepository
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory
from agentmesh.runtime_sdk import RuntimeAssignment
from agentmesh.runtime_sdk.builtin import (
    builtin_langgraph_version_id,
    langgraph_v2_descriptor,
)
from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run coordinated dispatch PostgreSQL tests",
    ),
]


def _factory(engine):
    return SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )


def _fixture(engine):
    now = datetime.now(timezone.utc)
    tenant_id = f"dispatch-pg-{uuid4().hex}"
    task = Task.create(
        tenant_id=tenant_id,
        objective="coordinated dispatch integration",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest=f"sha256:{uuid4().hex}{uuid4().hex}",
        max_concurrency=2,
    )
    task.start_coordination(at=now)
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="executor",
        objective="execute",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    agent_definition_id = uuid4()
    agent_version_id = uuid4()
    agent_version_digest = "a" * 64
    version_id = builtin_langgraph_version_id("v2")
    run = TaskRun.request(
        task.id,
        "integration-agent",
        agent_version_id=agent_version_id,
        agent_version_digest=agent_version_digest,
        subtask_id=subtask.id,
        runtime_version_id=version_id,
        runtime_authority="managed",
        at=now + timedelta(seconds=1),
    )
    subtask.queue(run.id, at=now + timedelta(seconds=2))
    subtask.start(run.id, at=now + timedelta(seconds=3))
    run.start(at=now + timedelta(seconds=3))
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="integration-worker",
        fencing_token=11,
        lease_expires_at=now + timedelta(minutes=30),
    )
    command_now = datetime.now(timezone.utc) + timedelta(seconds=1)

    factory = _factory(engine)
    with Session(engine) as session, session.begin():
        session.add(
            AgentDefinitionRecord(
                id=agent_definition_id,
                tenant_id=tenant_id,
                owner_id="dispatch-integration",
                name=f"dispatch-{uuid4().hex[:16]}",
                description="coordinated dispatch integration fixture",
                visibility="PRIVATE",
                lifecycle="ACTIVE",
                default_version_id=None,
                tags=[],
                version=1,
                created_at=now,
                updated_at=now,
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
                instructions="dispatch integration",
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
                execution_modes=["inline", "managed_async"],
                compatibility={},
                created_at=now,
                updated_at=now,
                published_at=now,
                revoked_at=None,
                revoke_reason=None,
            )
        )
        session.flush()
        session.add(SqlAlchemyTaskRepository._to_record(task))
        session.flush()
        session.add(SqlAlchemySubtaskRepository._to_record(subtask))
        session.flush()
        session.add(SqlAlchemyTaskRunRepository._to_record(run))
        session.flush()
        session.add(SqlAlchemyTaskAttemptRepository._to_record(attempt))
    assignment = RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id=tenant_id,
        task_id=str(task.id),
        run_id=str(run.id),
        agent_definition_id=str(agent_definition_id),
        agent_version_id=str(agent_version_id),
        agent_version_digest=agent_version_digest,
        runtime_version_id=str(version_id),
        runtime_descriptor_digest=RuntimeDescriptor.from_dict(
            langgraph_v2_descriptor()
        ).digest(),
        execution_mode="inline",
        run_role="EXECUTOR",
        revision=0,
        structured_input={"source": "postgres"},
        correlation_ids={"runtime_execution_id": str(run.runtime_execution_intent_id)},
    )
    return SimpleNamespace(
        tenant_id=tenant_id,
        task=task,
        run=run,
        attempt=attempt,
        assignment=assignment,
        factory=factory,
        now=command_now,
        agent_definition_id=agent_definition_id,
        agent_version_id=agent_version_id,
    )


def _cleanup(engine, fixture) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE task_runs SET runtime_execution_id = NULL WHERE task_id = :task_id"),
            {"task_id": fixture.task.id},
        )
        connection.execute(
            text(
                "DELETE FROM runtime_executions WHERE run_id IN "
                "(SELECT id FROM task_runs WHERE task_id = :task_id)"
            ),
            {"task_id": fixture.task.id},
        )
        connection.execute(
            text("DELETE FROM coordination_runtime_drains WHERE task_id = :task_id"),
            {"task_id": fixture.task.id},
        )
        connection.execute(
            text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": fixture.task.id}
        )
        connection.execute(
            text("DELETE FROM agent_versions WHERE id = :id"),
            {"id": fixture.agent_version_id},
        )
        connection.execute(
            text("DELETE FROM agent_definitions WHERE id = :id"),
            {"id": fixture.agent_definition_id},
        )


def _service(fixture) -> CoordinatedRuntimeDispatchService:
    return CoordinatedRuntimeDispatchService(uow_factory=fixture.factory)


def _prepare(fixture, *, assignment=None, now=None):
    return _service(fixture).prepare_runtime_assignment(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        attempt_id=fixture.attempt.id,
        fencing_token=fixture.attempt.fencing_token,
        assignment=assignment or fixture.assignment,
        now=now or fixture.now,
    )


def test_postgres_prepare_persists_and_exact_replay_is_immutable():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    try:
        prepared = _prepare(fixture)
        assert prepared.kind is CoordinatedRuntimePrepareKind.PREPARED
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            run = session.get(TaskRunRecord, fixture.run.id)
            snapshot = session.scalar(
                select(RuntimeAssignmentSnapshotRecord).where(
                    RuntimeAssignmentSnapshotRecord.runtime_execution_id == prepared.execution_id
                )
            )
            assert execution is not None and run is not None and snapshot is not None
            before = (
                execution.version,
                execution.created_at,
                execution.updated_at,
                execution.assignment_id,
                execution.assignment_digest,
                execution.current_owner_attempt_id,
                execution.current_fencing_token,
                run.runtime_execution_id,
                snapshot.created_at,
                snapshot.canonical_payload,
            )
        replay = _prepare(fixture, now=fixture.now + timedelta(minutes=1))
        assert replay.kind is CoordinatedRuntimePrepareKind.REPLAY
        with Session(engine) as session:
            execution = session.get(RuntimeExecutionRecord, prepared.execution_id)
            run = session.get(TaskRunRecord, fixture.run.id)
            snapshot = session.scalar(
                select(RuntimeAssignmentSnapshotRecord).where(
                    RuntimeAssignmentSnapshotRecord.runtime_execution_id == prepared.execution_id
                )
            )
            after = (
                execution.version,
                execution.created_at,
                execution.updated_at,
                execution.assignment_id,
                execution.assignment_digest,
                execution.current_owner_attempt_id,
                execution.current_fencing_token,
                run.runtime_execution_id,
                snapshot.created_at,
                snapshot.canonical_payload,
            )
            assert execution.phase == RuntimeExecutionPhase.PREPARED.value
            assert before == after
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_active_drain_blocks_before_prepare_without_rows():
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    try:
        drain = CoordinationRuntimeDrain.start(
            drain_id=uuid4(),
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            triggering_run_id=fixture.run.id,
            target=CoordinationRuntimeDrainTarget.RUNNING,
            reason="integration drain",
            at=fixture.now,
        )
        with fixture.factory() as uow:
            uow.coordination_runtime_drains.add(drain)
            uow.commit()
        result = _prepare(fixture)
        assert result.kind is CoordinatedRuntimePrepareKind.BLOCKED_BY_DRAIN
        with Session(engine) as session:
            assert (
                session.get(RuntimeExecutionRecord, fixture.run.runtime_execution_intent_id)
                is None
            )
            run = session.get(TaskRunRecord, fixture.run.id)
            assert run is not None and run.runtime_execution_id is None
            assert session.scalar(
                select(RuntimeAssignmentSnapshotRecord).where(
                    RuntimeAssignmentSnapshotRecord.tenant_id == fixture.tenant_id
                )
            ) is None
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_prepare_rolls_back_all_three_rows_when_snapshot_write_fails(monkeypatch):
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    try:
        def fail_snapshot(self, value):
            raise RuntimeError("injected snapshot failure")

        monkeypatch.setattr(SqlAlchemyRuntimeRepository, "add_assignment_snapshot", fail_snapshot)
        with pytest.raises(RuntimeError, match="injected snapshot failure"):
            _prepare(fixture)
        with Session(engine) as session:
            assert (
                session.get(RuntimeExecutionRecord, fixture.run.runtime_execution_intent_id)
                is None
            )
            run = session.get(TaskRunRecord, fixture.run.id)
            assert run is not None and run.runtime_execution_id is None
            assert session.scalar(
                select(RuntimeAssignmentSnapshotRecord).where(
                    RuntimeAssignmentSnapshotRecord.tenant_id == fixture.tenant_id
                )
            ) is None
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_concurrent_prepare_has_one_execution_and_one_replay():
    engine = create_engine(get_settings().database_url, pool_size=4, max_overflow=0)
    fixture = _fixture(engine)
    try:
        def call():
            return _prepare(fixture)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: call(), range(2)))
        assert sorted(result.kind for result in results) == sorted(
            [CoordinatedRuntimePrepareKind.PREPARED, CoordinatedRuntimePrepareKind.REPLAY]
        )
        with Session(engine) as session:
            assert session.scalar(
                select(RuntimeExecutionRecord.id).join(
                    TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id
                ).where(TaskRunRecord.task_id == fixture.task.id)
            ) is not None
            assert session.query(RuntimeExecutionRecord).join(
                TaskRunRecord, TaskRunRecord.id == RuntimeExecutionRecord.run_id
            ).filter(TaskRunRecord.task_id == fixture.task.id).count() == 1
            assert session.query(RuntimeAssignmentSnapshotRecord).filter(
                RuntimeAssignmentSnapshotRecord.tenant_id == fixture.tenant_id
            ).count() == 1
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


@pytest.mark.parametrize("mutation", ["bytes", "identity"])
def test_postgres_changed_assignment_is_rejected_without_mutating_prepared_rows(mutation):
    engine = create_engine(get_settings().database_url)
    fixture = _fixture(engine)
    try:
        prepared = _prepare(fixture)
        changed = dict(fixture.assignment.to_dict())
        if mutation == "bytes":
            changed["objective"] = "changed bytes"
        else:
            changed["assignment_id"] = str(uuid4())
        changed.pop("assignment_digest", None)
        changed_assignment = RuntimeAssignment.from_dict(changed)
        with Session(engine) as session:
            before = session.get(RuntimeExecutionRecord, prepared.execution_id)
            assert before is not None
            before_state = (
                before.version,
                before.updated_at,
                before.assignment_id,
                before.assignment_digest,
            )
        with pytest.raises(RuntimeExecutionConflict):
            _prepare(fixture, assignment=changed_assignment, now=fixture.now + timedelta(minutes=1))
        with Session(engine) as session:
            after = session.get(RuntimeExecutionRecord, prepared.execution_id)
            assert after is not None
            assert before_state == (
                after.version,
                after.updated_at,
                after.assignment_id,
                after.assignment_digest,
            )
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
