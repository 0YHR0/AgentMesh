"""Real PostgreSQL qualification for the d3 known-terminal convergence command."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedKnownTerminalKind,
    CoordinatedRuntimeConvergenceService,
)
from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedRuntimeDispatchService,
    CoordinatedRuntimePrepareKind,
)
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import Subtask
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.quotas import QuotaPolicy, QuotaReservation, QuotaScope
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import TaskAttempt, TaskRun
from agentmesh.infrastructure.postgres.models import (
    AgentDefinitionRecord,
    AgentVersionRecord,
    TaskAttemptRecord,
    TaskRecord,
)
from agentmesh.infrastructure.postgres.runtime_repositories import SqlAlchemyRuntimeRepository
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _cleanup as _dispatch_cleanup,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _fixture as _dispatch_fixture,
)
from tests.integration.test_coordinated_runtime_dispatch_postgres import (
    _prepare as _dispatch_prepare,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run convergence PostgreSQL tests",
    ),
]

UTC = timezone.utc


class _RecordingScheduler:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, datetime, object]] = []
        self.fail = fail

    def schedule(self, uow, task, *, at, causation_id):
        self.calls.append(("schedule", at, causation_id))
        if self.fail:
            raise RuntimeError("injected scheduler failure")
        return []


def _running_fixture(engine):
    fixture = _dispatch_fixture(engine)
    prepared = _dispatch_prepare(fixture)
    assert prepared.kind is CoordinatedRuntimePrepareKind.PREPARED
    now = fixture.now + timedelta(seconds=10)
    with fixture.factory() as uow:
        execution = uow.runtimes.get_execution(
            prepared.execution_id, tenant_id=fixture.tenant_id, for_update=True
        )
        assert execution is not None
        execution = execution.apply_observation(
            phase=RuntimeExecutionPhase.RUNNING,
            provider_sequence=1,
            now=now,
        )
        uow.runtimes.save_execution(execution, tenant_id=fixture.tenant_id)
        uow.commit()
    return fixture, execution, now


def _observation(execution, *, phase: RuntimePhase, observed_at: datetime):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=observed_at,
        provider_event_id=f"convergence-pg-{uuid4().hex}",
        provider_sequence=2,
        output={"ok": True} if phase is RuntimePhase.SUCCEEDED else None,
    )


def _service(fixture, scheduler):
    return CoordinatedRuntimeConvergenceService(
        uow_factory=fixture.factory,
        coordinated_scheduler=scheduler,
        cancel_deadline_window=timedelta(minutes=5),
    )


def _cleanup(engine, fixture):
    """Remove convergence-only rows before the shared dispatch fixture cleanup."""
    with engine.begin() as connection:
        # The relay claims pending events globally, not by tenant.  Remove
        # every event for this fixture before deleting its task so a later
        # integration test cannot publish stale run messages into its stream.
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = :tenant_id"),
            {"tenant_id": fixture.tenant_id},
        )
        connection.execute(
            text(
                "DELETE FROM quota_reservations WHERE policy_id IN "
                "(SELECT id FROM quota_policies WHERE tenant_id = :tenant_id)"
            ),
            {"tenant_id": fixture.tenant_id},
        )
        connection.execute(
            text("DELETE FROM quota_policies WHERE tenant_id = :tenant_id"),
            {"tenant_id": fixture.tenant_id},
        )
    _dispatch_cleanup(engine, fixture)


def _qualify_real_scheduler(engine, fixture):
    """Make the fixture's published test agent satisfy the real scheduler contract."""
    with Session(engine) as session, session.begin():
        definition = session.get(AgentDefinitionRecord, fixture.agent_definition_id)
        version = session.get(AgentVersionRecord, fixture.agent_version_id)
        assert definition is not None and version is not None
        # The shared dispatch fixture intentionally leaves its definition
        # unpublished and gives it a unique name.  The production scheduler
        # resolves by agent name, so publish this exact fixture identity for
        # the real-scheduler qualification only.
        definition.name = "integration-agent"
        definition.default_version_id = fixture.agent_version_id
        version.verified_capabilities = ["general.task", "general.supervise"]
        version.execution_modes = ["inline", "managed_async", "async"]


def _real_scheduler(engine, fixture):
    _qualify_real_scheduler(engine, fixture)
    return CoordinatedScheduler(supervisor_agent_id="integration-agent")


def _add_quota_reservation(fixture):
    policy = QuotaPolicy.create(
        tenant_id=fixture.tenant_id,
        scope=QuotaScope.TENANT,
        project_id=None,
        max_concurrent_attempts=10,
        weight=1,
        version=1,
        created_by="postgres-qualification",
    )
    reservation = QuotaReservation.acquire(
        policy_id=policy.id,
        attempt_id=fixture.attempt.id,
        tenant_id=fixture.tenant_id,
        project_id=fixture.task.project_id,
    )
    with fixture.factory() as uow:
        uow.quotas.add_policy(policy)
        uow.quotas.add_reservation(reservation)
        uow.commit()
    return policy, reservation


def _add_budget_reservation(engine, fixture):
    """Install the persisted budget/accounting state the shared fixture omits."""
    budget = TaskBudget.create(
        max_attempts=10,
        max_tokens=100,
        token_reservation_per_attempt=25,
        max_cost_micros=1000,
        cost_reservation_micros_per_attempt=100,
    )
    with Session(engine) as session, session.begin():
        task = session.get(TaskRecord, fixture.task.id)
        attempt = session.get(TaskAttemptRecord, fixture.attempt.id)
        assert task is not None and attempt is not None
        task.budget = budget.to_dict()
        task.reserved_tokens = budget.token_reservation_per_attempt
        task.reserved_cost_micros = budget.cost_reservation_micros_per_attempt
        attempt.reserved_tokens = budget.token_reservation_per_attempt
        attempt.reserved_cost_micros = budget.cost_reservation_micros_per_attempt


def _budget_projection(engine, fixture):
    with engine.connect() as connection:
        task = tuple(
            connection.execute(
                text(
                    "SELECT reserved_tokens, settled_tokens, reserved_cost_micros, "
                    "settled_cost_micros FROM tasks WHERE id = :task_id"
                ),
                {"task_id": fixture.task.id},
            ).one()
        )
        attempt = tuple(
            connection.execute(
                text(
                    "SELECT reserved_tokens, settled_tokens, reserved_cost_micros, "
                    "settled_cost_micros, budget_settlement_source "
                    "FROM task_attempts WHERE id = :attempt_id"
                ),
                {"attempt_id": fixture.attempt.id},
            ).one()
        )
        quota = tuple(
            connection.execute(
                text(
                    "SELECT released_at FROM quota_reservations "
                    "WHERE attempt_id = :attempt_id ORDER BY id"
                ),
                {"attempt_id": fixture.attempt.id},
            ).all()
        )
    return task, attempt, quota


def _add_sibling(engine, fixture, *, state):
    """Persist one sibling at each real Runtime boundary used by the barrier."""
    # Keep sibling transitions before the target receipt clock used by the
    # convergence call; domain transitions reject backwards policy time.
    clock = fixture.now + timedelta(seconds=1)
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=fixture.task.id,
        key=f"sibling-{state.lower()}-{uuid4().hex[:8]}",
        objective="qualification sibling",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    run = TaskRun.request(
        fixture.task.id,
        "integration-agent",
        agent_version_id=fixture.agent_version_id,
        agent_version_digest="a" * 64,
        subtask_id=subtask.id,
        runtime_version_id=fixture.assignment.runtime_version_id,
        runtime_authority="managed",
        at=clock,
    )
    subtask.queue(run.id, at=clock)
    attempt = None
    if state != "queued":
        subtask.start(run.id, at=clock + timedelta(seconds=1))
        run.start(at=clock + timedelta(seconds=1))
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id=f"sibling-{state.lower()}",
            fencing_token=17,
            lease_expires_at=clock + timedelta(hours=1),
        )
    with fixture.factory() as uow:
        uow.subtasks.add(subtask)
        uow.runs.add(run)
        uow.commit()
    # The repository unit of work does not currently express the SQL foreign
    # key dependency from an attempt to its run.  Persist the parent chain
    # first, then lease the attempt in a second transaction so this fixture
    # exercises the same real PostgreSQL constraints as production writes.
    if attempt is not None:
        with fixture.factory() as uow:
            uow.attempts.add(attempt)
            uow.commit()

    if state in {"queued", "no-execution"}:
        return subtask, run, attempt

    assignment = replace(
        fixture.assignment,
        assignment_id=str(uuid4()),
        run_id=str(run.id),
        structured_input={"source": "postgres", "sibling_state": state},
        correlation_ids={"runtime_execution_id": str(run.runtime_execution_intent_id)},
        assignment_digest=None,
    )
    dispatch_service = CoordinatedRuntimeDispatchService(uow_factory=fixture.factory)
    prepared = dispatch_service.prepare_runtime_assignment(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        assignment=assignment,
        now=clock + timedelta(seconds=2),
    )
    assert prepared.execution_id is not None
    # The prepare command persists immutable Run admission fields in its own
    # UoW.  Reload the domain object before applying a later sibling terminal
    # transition; mutating the stale fixture copy would be rejected by the
    # repository's immutable-field guard.
    with fixture.factory() as uow:
        persisted_run = uow.runs.get(run.id)
        assert persisted_run is not None
        run = persisted_run
    if state == "prepared":
        return subtask, run, attempt
    crossed = dispatch_service.cross_runtime_dispatch_boundary(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        runtime_execution_id=prepared.execution_id,
        assignment_digest=assignment.assignment_digest,
        now=clock + timedelta(seconds=3),
    )
    assert crossed.execution_id == prepared.execution_id
    with fixture.factory() as uow:
        execution = uow.runtimes.get_execution(
            prepared.execution_id, tenant_id=fixture.tenant_id, for_update=True
        )
        assert execution is not None
        at = clock + timedelta(seconds=4)
        if state == "active":
            execution = execution.apply_observation(
                phase=RuntimeExecutionPhase.RUNNING, provider_sequence=1, now=at
            )
        elif state == "reconciliation":
            execution = execution.apply_observation(
                phase=RuntimeExecutionPhase.OUTCOME_UNKNOWN, provider_sequence=1, now=at
            )
            attempt.mark_outcome_unknown("provider response was lost", at=at)
            run.require_runtime_reconciliation("provider response was lost", at=at)
            subtask.require_runtime_reconciliation(run.id, "provider response was lost", at=at)
            uow.attempts.save(attempt)
            uow.runs.save(run)
            uow.subtasks.save(subtask)
        elif state == "known-terminal":
            execution = execution.apply_observation(
                phase=RuntimeExecutionPhase.SUCCEEDED, provider_sequence=1, now=at
            )
            attempt.succeed(at=at)
            run.succeed({"sibling": "done"}, at=at)
            subtask.complete(run.id, {"sibling": "done"}, at=at)
            uow.attempts.save(attempt)
            uow.runs.save(run)
            uow.subtasks.save(subtask)
        else:
            raise AssertionError(f"unknown sibling state: {state}")
        uow.runtimes.save_execution(execution, tenant_id=fixture.tenant_id)
        uow.commit()
    return subtask, run, attempt


def _counts(engine, fixture):
    with engine.connect() as connection:
        return {
            name: connection.scalar(
                text(query), {"task_id": fixture.task.id, "tenant_id": fixture.tenant_id}
            )
            for name, query in {
                "observations": (
                    "SELECT count(*) FROM runtime_observations "
                    "WHERE tenant_id = :tenant_id"
                ),
                "drains": (
                    "SELECT count(*) FROM coordination_runtime_drains "
                    "WHERE task_id = :task_id"
                ),
                "lifecycle": (
                    "SELECT count(*) FROM runtime_lifecycle_operations "
                    "WHERE tenant_id = :tenant_id"
                ),
                "outbox": "SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant_id",
                "executions": (
                    "SELECT count(*) FROM runtime_executions WHERE tenant_id = :tenant_id"
                ),
                "runs": "SELECT count(*) FROM task_runs WHERE task_id = :task_id",
                "active_quota": (
                    "SELECT count(*) FROM quota_reservations qr "
                    "JOIN quota_policies qp ON qp.id = qr.policy_id "
                    "WHERE qp.tenant_id = :tenant_id AND qr.released_at IS NULL"
                ),
            }.items()
        }


def test_postgres_success_persists_and_exact_replay_is_read_only():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    scheduler = _RecordingScheduler()
    service = _service(fixture, scheduler)
    observation = _observation(
        execution, phase=RuntimePhase.SUCCEEDED, observed_at=now + timedelta(seconds=1)
    )
    try:
        first = service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now + timedelta(seconds=2),
            causation_id=uuid4(),
        )
        assert first.kind is CoordinatedKnownTerminalKind.APPLIED
        assert len(scheduler.calls) == 1
        before = _counts(engine, fixture)
        with Session(engine) as session:
            phase = session.scalar(
                text("SELECT phase FROM runtime_executions WHERE id = :id"),
                {"id": execution.id},
            )
            run_status = session.scalar(
                text("SELECT status FROM task_runs WHERE id = :id"), {"id": fixture.run.id}
            )
            attempt_status = session.scalar(
                text("SELECT status FROM task_attempts WHERE id = :id"),
                {"id": fixture.attempt.id},
            )
        assert phase == RuntimeExecutionPhase.SUCCEEDED.value
        assert run_status == "SUCCEEDED"
        assert attempt_status == "SUCCEEDED"
        assert before["observations"] == 1
        replay = service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now + timedelta(seconds=3),
            causation_id=uuid4(),
        )
        assert replay.kind is CoordinatedKnownTerminalKind.REPLAY
        assert _counts(engine, fixture) == before
        assert len(scheduler.calls) == 1
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


@pytest.mark.parametrize(
    ("phase", "expected_execution"),
    [
        (RuntimePhase.FAILED, RuntimeExecutionPhase.FAILED),
        (RuntimePhase.TIMED_OUT, RuntimeExecutionPhase.TIMED_OUT),
    ],
)
def test_postgres_failed_and_timed_out_complete_deterministic_drain(
    phase, expected_execution
):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    scheduler = _RecordingScheduler()
    service = _service(fixture, scheduler)
    observation = _observation(execution, phase=phase, observed_at=now + timedelta(seconds=1))
    try:
        result = service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now + timedelta(seconds=2),
            causation_id=uuid4(),
        )
        assert result.kind is CoordinatedKnownTerminalKind.APPLIED
        assert result.drain_id is not None
        assert scheduler.calls == []
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT phase FROM runtime_executions WHERE id = :id"),
                {"id": execution.id},
            ) == expected_execution.value
            assert connection.scalar(
                text("SELECT status FROM tasks WHERE id = :task_id"),
                {"task_id": fixture.task.id},
            ) == "FAILED"
            assert connection.scalar(
                text("SELECT status FROM coordination_runtime_drains WHERE task_id = :task_id"),
                {"task_id": fixture.task.id},
            ) == "COMPLETE"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_success_settles_budget_and_quota_once_on_exact_replay():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    _add_budget_reservation(engine, fixture)
    _add_quota_reservation(fixture)
    scheduler = _RecordingScheduler()
    service = _service(fixture, scheduler)
    observation = _observation(
        execution, phase=RuntimePhase.SUCCEEDED, observed_at=now + timedelta(seconds=1)
    )
    try:
        initial = _budget_projection(engine, fixture)
        assert initial[0] == (25, 0, 100, 0)
        assert initial[1] == (25, None, 100, None, None)
        first = service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now + timedelta(seconds=2),
            causation_id=uuid4(),
        )
        assert first.kind is CoordinatedKnownTerminalKind.APPLIED
        settled = _budget_projection(engine, fixture)
        assert settled[0] == (0, 25, 0, 100)
        assert settled[1] == (25, 25, 100, 100, "CONSERVATIVE_ESTIMATE")
        assert len(settled[2]) == 1 and settled[2][0][0] is not None
        replay = service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now + timedelta(seconds=3),
            causation_id=uuid4(),
        )
        assert replay.kind is CoordinatedKnownTerminalKind.REPLAY
        assert _budget_projection(engine, fixture) == settled
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_cancel_without_stable_intent_is_applied_as_failure():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    scheduler = _RecordingScheduler()
    service = _service(fixture, scheduler)
    observation = _observation(execution, phase=RuntimePhase.CANCELED, observed_at=now)
    try:
        result = service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now + timedelta(seconds=1),
            causation_id=uuid4(),
        )
        assert result.kind is CoordinatedKnownTerminalKind.APPLIED
        assert scheduler.calls == []
        reason = "runtime.unrequested_cancellation"
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT phase FROM runtime_executions WHERE id = :id"),
                {"id": execution.id},
            ) == RuntimeExecutionPhase.CANCELED.value
            assert connection.scalar(
                text("SELECT status FROM task_attempts WHERE id = :id"),
                {"id": fixture.attempt.id},
            ) == "FAILED"
            assert connection.scalar(
                text("SELECT status FROM task_runs WHERE id = :id"),
                {"id": fixture.run.id},
            ) == "FAILED"
            assert connection.scalar(
                text("SELECT status FROM subtasks WHERE id = :id"),
                {"id": fixture.run.subtask_id},
            ) == "FAILED"
            assert connection.scalar(
                text("SELECT status FROM tasks WHERE id = :id"),
                {"id": fixture.task.id},
            ) == "FAILED"
            assert connection.scalar(
                text("SELECT error FROM tasks WHERE id = :id"),
                {"id": fixture.task.id},
            ) == reason
            assert connection.scalar(
                text("SELECT reason FROM coordination_runtime_drains WHERE task_id = :id"),
                {"id": fixture.task.id},
            ) == reason
            assert connection.scalar(
                text("SELECT status FROM coordination_runtime_drains WHERE task_id = :id"),
                {"id": fixture.task.id},
            ) == "COMPLETE"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_scheduler_failure_rolls_back_evidence_execution_and_task():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    _add_budget_reservation(engine, fixture)
    _add_quota_reservation(fixture)
    scheduler = _RecordingScheduler(fail=True)
    service = _service(fixture, scheduler)
    observation = _observation(
        execution, phase=RuntimePhase.SUCCEEDED, observed_at=now + timedelta(seconds=1)
    )
    try:
        before = _counts(engine, fixture)
        before_budget = _budget_projection(engine, fixture)
        with pytest.raises(RuntimeError, match="injected scheduler failure"):
            service.apply_known_terminal(
                tenant_id=fixture.tenant_id,
                task_id=fixture.task.id,
                run_id=fixture.run.id,
                attempt_id=fixture.attempt.id,
                fencing_token=fixture.attempt.fencing_token,
                runtime_execution_id=execution.id,
                observation=observation,
                received_at=now + timedelta(seconds=2),
                causation_id=uuid4(),
            )
        assert _counts(engine, fixture) == before
        assert _budget_projection(engine, fixture) == before_budget
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT phase FROM runtime_executions WHERE id = :id"),
                {"id": execution.id},
            ) == RuntimeExecutionPhase.RUNNING.value
            assert connection.scalar(
                text("SELECT status FROM task_runs WHERE id = :id"), {"id": fixture.run.id}
            ) == "RUNNING"
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_real_scheduler_creates_one_supervisor_and_replay_does_not_reschedule():
    """The qualification must exercise the production scheduler, not a recording double."""
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"], pool_size=4, max_overflow=0)
    fixture, execution, now = _running_fixture(engine)
    scheduler = _real_scheduler(engine, fixture)
    service = _service(fixture, scheduler)
    observation = _observation(
        execution, phase=RuntimePhase.SUCCEEDED, observed_at=now + timedelta(seconds=1)
    )
    try:
        def deliver(_):
            return service.apply_known_terminal(
                tenant_id=fixture.tenant_id,
                task_id=fixture.task.id,
                run_id=fixture.run.id,
                attempt_id=fixture.attempt.id,
                fencing_token=fixture.attempt.fencing_token,
                runtime_execution_id=execution.id,
                observation=observation,
                received_at=now + timedelta(seconds=2),
                causation_id=uuid4(),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(deliver, range(2)))
        assert sorted(value.kind for value in results) == sorted(
            [CoordinatedKnownTerminalKind.APPLIED, CoordinatedKnownTerminalKind.REPLAY]
        )
        applied = next(
            value for value in results if value.kind is CoordinatedKnownTerminalKind.APPLIED
        )
        replay = next(
            value for value in results if value.kind is CoordinatedKnownTerminalKind.REPLAY
        )
        assert applied.scheduled_run_ids
        assert replay.scheduled_run_ids == ()
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM task_runs WHERE task_id = :task_id "
                    "AND role = 'SUPERVISOR'"
                ),
                {"task_id": fixture.task.id},
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant_id "
                    "AND topic = 'agentmesh.run.requested'"
                ),
                {"tenant_id": fixture.tenant_id},
            ) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM runtime_observations WHERE tenant_id = :tenant_id"),
                {"tenant_id": fixture.tenant_id},
            ) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM coordination_runtime_drains WHERE task_id = :task_id"),
                {"task_id": fixture.task.id},
            ) == 0
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


@pytest.mark.parametrize(
    ("state", "expected_kind", "expected_task", "expected_drain"),
    [
        ("queued", CoordinatedKnownTerminalKind.APPLIED, "FAILED", "COMPLETE"),
        ("no-execution", CoordinatedKnownTerminalKind.APPLIED, "FAILED", "COMPLETE"),
        ("prepared", CoordinatedKnownTerminalKind.APPLIED, "FAILED", "COMPLETE"),
        ("active", CoordinatedKnownTerminalKind.DRAINING_ACTIVE, "RUNNING", "DRAINING"),
        (
            "reconciliation",
            CoordinatedKnownTerminalKind.DRAINING_RECONCILIATION,
            "RECONCILIATION_REQUIRED",
            "DRAINING",
        ),
        ("known-terminal", CoordinatedKnownTerminalKind.APPLIED, "FAILED", "COMPLETE"),
    ],
)
def test_postgres_failure_matrix_drains_every_real_sibling_boundary(
    state, expected_kind, expected_task, expected_drain
):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    _add_sibling(engine, fixture, state=state)
    _add_quota_reservation(fixture)
    service = _service(fixture, _RecordingScheduler())
    observation = _observation(
        execution, phase=RuntimePhase.FAILED, observed_at=now + timedelta(seconds=1)
    )
    try:
        result = service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now + timedelta(seconds=2),
            causation_id=uuid4(),
        )
        assert result.kind is expected_kind
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT status FROM tasks WHERE id = :task_id"),
                {"task_id": fixture.task.id},
            ) == expected_task
            assert connection.scalar(
                text("SELECT status FROM coordination_runtime_drains WHERE task_id = :task_id"),
                {"task_id": fixture.task.id},
            ) == expected_drain
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM quota_reservations qr "
                    "JOIN quota_policies qp ON qp.id = qr.policy_id "
                    "WHERE qp.tenant_id = :tenant_id AND qr.released_at IS NULL"
                ),
                {"tenant_id": fixture.tenant_id},
            ) == 0
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_observation_identity_digest_and_multiple_anchor_conflicts_are_fail_closed():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    service = _service(fixture, _RecordingScheduler())
    observation = _observation(
        execution, phase=RuntimePhase.SUCCEEDED, observed_at=now + timedelta(seconds=1)
    )
    try:
        first = service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now + timedelta(seconds=2),
            causation_id=uuid4(),
        )
        assert first.kind is CoordinatedKnownTerminalKind.APPLIED
        before = _counts(engine, fixture)
        changed = replace(observation, output={"tampered": True})
        with pytest.raises(RuntimeExecutionConflict, match="identity conflicts"):
            service.apply_known_terminal(
                tenant_id=fixture.tenant_id,
                task_id=fixture.task.id,
                run_id=fixture.run.id,
                attempt_id=fixture.attempt.id,
                fencing_token=fixture.attempt.fencing_token,
                runtime_execution_id=execution.id,
                observation=changed,
                received_at=now + timedelta(seconds=3),
                causation_id=uuid4(),
            )
        # A second accepted anchor with the same digest but another ID models
        # a partial/corrupt projection without weakening the production validator.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO runtime_observations "
                    "(id, tenant_id, runtime_execution_id, observation_id, observation_digest, "
                    "assignment_id, assignment_digest, provider_event_id, provider_sequence, "
                    "phase, observed_at, received_at, safe_summary, evidence, "
                    "processing_outcome, processing_version) "
                    "SELECT :id, tenant_id, runtime_execution_id, :observation_id, "
                    "observation_digest, assignment_id, assignment_digest, provider_event_id, "
                    "provider_sequence, phase, observed_at, received_at, safe_summary, evidence, "
                    "processing_outcome, processing_version FROM runtime_observations "
                    "WHERE runtime_execution_id = :execution_id"
                ),
                {
                    "id": uuid4(),
                    "observation_id": f"different-id-{uuid4().hex}",
                    "execution_id": execution.id,
                },
            )
        with pytest.raises(RuntimeExecutionConflict, match="projection is incomplete"):
            service.apply_known_terminal(
                tenant_id=fixture.tenant_id,
                task_id=fixture.task.id,
                run_id=fixture.run.id,
                attempt_id=fixture.attempt.id,
                fencing_token=fixture.attempt.fencing_token,
                runtime_execution_id=execution.id,
                observation=observation,
                received_at=now + timedelta(seconds=4),
                causation_id=uuid4(),
            )
        assert _counts(engine, fixture)["observations"] == before["observations"] + 1
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_partial_success_projection_is_rejected_without_new_evidence():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    service = _service(fixture, _RecordingScheduler())
    observation = _observation(
        execution, phase=RuntimePhase.SUCCEEDED, observed_at=now + timedelta(seconds=1)
    )
    try:
        service.apply_known_terminal(
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            run_id=fixture.run.id,
            attempt_id=fixture.attempt.id,
            fencing_token=fixture.attempt.fencing_token,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now + timedelta(seconds=2),
            causation_id=uuid4(),
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE task_runs SET output = CAST(:output AS jsonb) "
                    "WHERE id = :run_id"
                ),
                {"output": '{"partial": true}', "run_id": fixture.run.id},
            )
        before = _counts(engine, fixture)
        with pytest.raises(RuntimeExecutionConflict, match="success projection differs"):
            service.apply_known_terminal(
                tenant_id=fixture.tenant_id,
                task_id=fixture.task.id,
                run_id=fixture.run.id,
                attempt_id=fixture.attempt.id,
                fencing_token=fixture.attempt.fencing_token,
                runtime_execution_id=execution.id,
                observation=observation,
                received_at=now + timedelta(seconds=3),
                causation_id=uuid4(),
            )
        assert _counts(engine, fixture) == before
    finally:
        _cleanup(engine, fixture)
        engine.dispose()


def test_postgres_write_failure_rolls_back_budget_and_quota(monkeypatch):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _running_fixture(engine)
    _add_budget_reservation(engine, fixture)
    _add_quota_reservation(fixture)
    service = _service(fixture, _RecordingScheduler())
    observation = _observation(
        execution, phase=RuntimePhase.SUCCEEDED, observed_at=now + timedelta(seconds=1)
    )

    def fail_observation(*args, **kwargs):
        raise RuntimeError("injected evidence write failure")

    monkeypatch.setattr(SqlAlchemyRuntimeRepository, "add_observation", fail_observation)
    try:
        before = _budget_projection(engine, fixture)
        with pytest.raises(RuntimeError, match="injected evidence write failure"):
            service.apply_known_terminal(
                tenant_id=fixture.tenant_id,
                task_id=fixture.task.id,
                run_id=fixture.run.id,
                attempt_id=fixture.attempt.id,
                fencing_token=fixture.attempt.fencing_token,
                runtime_execution_id=execution.id,
                observation=observation,
                received_at=now + timedelta(seconds=2),
                causation_id=uuid4(),
            )
        assert _budget_projection(engine, fixture) == before
    finally:
        _cleanup(engine, fixture)
        engine.dispose()
