"""Real PostgreSQL qualification for coordinated Runtime reconciliation (c2e3)."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from agentmesh.application.coordinated_runtime_reconciliation import (
    CoordinatedRuntimeReconciliationService,
    _reconciliation_scope,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedUnknownOutcomeKind,
)
from agentmesh.application.runtime_contracts import TerminalObservationValidator
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import (
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import RuntimeExecutionConflict
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.runtime_execution import (
    RuntimeLifecycleIntent,
    RuntimeLifecycleOperation,
    RuntimeLifecycleStatus,
)
from agentmesh.domain.tasks import TaskStatus
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.runtime_repositories import (
    SqlAlchemyRuntimeRepository,
)
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from tests.integration.test_coordinated_runtime_convergence_postgres import (
    _add_budget_reservation,
    _add_sibling,
    _budget_projection,
    _cleanup,
    _counts,
    _RecordingScheduler,
    _running_fixture,
)
from tests.integration.test_coordinated_runtime_unknown_postgres import (
    _add_quota_reservation,
    _supervisor_fixture,
)
from tests.integration.test_coordinated_runtime_unknown_postgres import (
    _deliver as _park_unknown,
)
from tests.integration.test_coordinated_runtime_unknown_postgres import (
    _observation as _unknown_observation,
)
from tests.integration.test_coordinated_runtime_unknown_postgres import (
    _service as _unknown_service,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run reconciliation PostgreSQL tests",
    ),
]


def _principal(tenant_id: str) -> PrincipalContext:
    return PrincipalContext(
        principal_id="postgres-reconciliation-operator",
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({Role.OPERATOR}),
        authenticated=True,
        authentication_method="integration-test",
    )


def _service(fixture, scheduler):
    return CoordinatedRuntimeReconciliationService(
        uow_factory=fixture.factory,
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
        ),
        coordinated_scheduler=scheduler,
        cancel_deadline_window=timedelta(minutes=5),
    )


def _terminal(execution, now, phase: RuntimePhase):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=now + timedelta(seconds=1),
        provider_event_id=f"reconciliation-pg-{uuid4().hex}",
        provider_sequence=3,
        output={"answer": 42} if phase is RuntimePhase.SUCCEEDED else None,
    )


def _reconcile(service, fixture, execution, observation, *, key="reconcile-1", at=None):
    at = at or observation.observed_at + timedelta(seconds=1)
    return service.reconcile_known_terminal(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        run_id=fixture.run.id,
        attempt_id=fixture.attempt.id,
        fencing_token=fixture.attempt.fencing_token,
        runtime_execution_id=execution.id,
        principal=_principal(fixture.tenant_id),
        observation=observation,
        evidence_digest=TerminalObservationValidator.digest(observation),
        evidence_reference="audit://postgres/reconciliation-proof",
        reason="postgres independently verified conclusion",
        idempotency_key=key,
        received_at=at,
    )


def _reconciliation_scope_for_fixture(fixture, execution):
    return _reconciliation_scope(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task.id,
        principal_id="postgres-reconciliation-operator",
        runtime_execution_id=execution.id,
    )


def _cleanup_reconciliation(engine, fixture, execution):
    """Remove reconciliation idempotency rows before shared fixture cleanup."""
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM idempotency_records WHERE scope = :scope"),
            {"scope": _reconciliation_scope_for_fixture(fixture, execution)},
        )
    _cleanup(engine, fixture)


def _parked(
    engine,
    *,
    budget=False,
    existing_drain_target=None,
    sibling_state=None,
    unknown_phase=RuntimePhase.OUTCOME_UNKNOWN,
):
    fixture, execution, now = _running_fixture(engine)
    if budget:
        _add_budget_reservation(engine, fixture)
    _add_quota_reservation(fixture)
    if sibling_state is not None:
        _add_sibling(engine, fixture, state=sibling_state)
    if existing_drain_target is not None:
        drain = CoordinationRuntimeDrain.start(
            drain_id=uuid4(),
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            triggering_run_id=fixture.run.id,
            target=existing_drain_target,
            reason="existing first cause",
            at=now,
        )
        with fixture.factory() as uow:
            uow.coordination_runtime_drains.add(drain)
            uow.commit()
    unknown = _unknown_observation(
        execution,
        phase=unknown_phase,
        observed_at=now + timedelta(seconds=1),
    )
    parked_result = _park_unknown(
        _unknown_service(fixture),
        fixture,
        execution,
        unknown,
        received_at=now + timedelta(seconds=2),
    )
    assert parked_result.kind is CoordinatedUnknownOutcomeKind.PARKED
    return fixture, execution, now


def _parked_supervisor(
    engine,
    *,
    unknown_phase,
    existing_drain_target=None,
):
    fixture, execution, now = _supervisor_fixture(engine)
    _add_budget_reservation(engine, fixture)
    _add_quota_reservation(fixture)
    if existing_drain_target is not None:
        drain = CoordinationRuntimeDrain.start(
            drain_id=uuid4(),
            tenant_id=fixture.tenant_id,
            task_id=fixture.task.id,
            triggering_run_id=fixture.run.id,
            target=existing_drain_target,
            reason="existing first cause",
            at=now,
        )
        with fixture.factory() as uow:
            uow.coordination_runtime_drains.add(drain)
            uow.commit()
    unknown = _unknown_observation(
        execution,
        phase=unknown_phase,
        observed_at=now + timedelta(seconds=1),
    )
    parked_result = _park_unknown(
        _unknown_service(fixture),
        fixture,
        execution,
        unknown,
        received_at=now + timedelta(seconds=2),
    )
    assert parked_result.kind.value in {
        "PARKED",
        "DRAINING_ACTIVE",
        "DRAINING_RECONCILIATION",
    }
    return fixture, execution, now, unknown


def _set_expired_budget(engine, fixture, *, deadline):
    budget = TaskBudget.create(
        max_attempts=10,
        max_tokens=100,
        token_reservation_per_attempt=25,
        max_cost_micros=1000,
        cost_reservation_micros_per_attempt=100,
        deadline=deadline,
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE tasks SET budget = CAST(:budget AS jsonb) WHERE id = :id"),
            {"budget": json.dumps(budget.to_dict()), "id": fixture.task.id},
        )


def _projection(engine, fixture, execution):
    with engine.connect() as connection:
        return {
            "task": tuple(
                connection.execute(
                    text(
                        "SELECT status, current_run_id, error, output, candidate_output "
                        "FROM tasks WHERE id = :id"
                    ),
                    {"id": fixture.task.id},
                ).one()
            ),
            "run": tuple(
                connection.execute(
                    text("SELECT status, error, output FROM task_runs WHERE id = :id"),
                    {"id": fixture.run.id},
                ).one()
            ),
            "attempt": tuple(
                connection.execute(
                    text(
                        "SELECT status, error, settled_tokens, settled_cost_micros, "
                        "budget_settlement_source FROM task_attempts WHERE id = :id"
                    ),
                    {"id": fixture.attempt.id},
                ).one()
            ),
            "execution": tuple(
                connection.execute(
                    text(
                        "SELECT phase, provider_sequence FROM runtime_executions WHERE id = :id"
                    ),
                    {"id": execution.id},
                ).one()
            ),
        }


def _qualification_projection(engine, fixture, execution):
    with engine.connect() as connection:
        budget_exhausted_reason = connection.scalar(
            text("SELECT budget_exhausted_reason FROM tasks WHERE id = :id"),
            {"id": fixture.task.id},
        )
        subtask = (
            tuple(
                connection.execute(
                    text("SELECT status, error, output FROM subtasks WHERE id = :id"),
                    {"id": fixture.run.subtask_id},
                ).one()
            )
            if fixture.run.subtask_id is not None
            else None
        )
        drains = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT id, target, reason, status FROM coordination_runtime_drains "
                    "WHERE task_id = :id ORDER BY created_at, id"
                ),
                {"id": fixture.task.id},
            ).all()
        )
        evidence = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT observation_id, observation_digest, phase, provider_sequence, "
                    "processing_outcome, evidence FROM runtime_observations "
                    "WHERE runtime_execution_id = :id ORDER BY received_at, id"
                ),
                {"id": execution.id},
            ).all()
        )
        resolutions = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT id, action, previous_status, resulting_status, details "
                    "FROM task_resolutions WHERE task_id = :id ORDER BY created_at, id"
                ),
                {"id": fixture.task.id},
            ).all()
        )
        events = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT topic, envelope FROM outbox_events WHERE tenant_id = :tenant "
                    "AND topic = 'agentmesh.runtime.outcome-reconciled' ORDER BY created_at, id"
                ),
                {"tenant": fixture.tenant_id},
            ).all()
        )
        idempotency = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT key, request_hash, result FROM idempotency_records "
                    "WHERE scope = :scope ORDER BY key"
                ),
                {"scope": _reconciliation_scope_for_fixture(fixture, execution)},
            ).all()
        )
    return {
        **_projection(engine, fixture, execution),
        "budget_exhausted_reason": budget_exhausted_reason,
        "subtask": subtask,
        "drains": drains,
        "evidence": evidence,
        "resolutions": resolutions,
        "events": events,
        "idempotency": idempotency,
        "budget": _budget_projection(engine, fixture),
    }


def _assert_reconciliation_records(
    projection,
    fixture,
    execution,
    unknown,
    observation,
    *,
    expected_task_status,
    expected_run_status,
    expected_attempt_status,
    expected_drain_target,
    expected_task_error,
    expected_task_output=None,
    expected_candidate_output=None,
    expected_run_output=None,
    output_quarantined=False,
    expected_current_run_id=None,
):
    if expected_current_run_id is None and expected_task_status != "WAITING_APPROVAL":
        expected_current_run_id = fixture.run.id
    assert projection["task"] == (
        expected_task_status,
        expected_current_run_id,
        expected_task_error,
        expected_task_output,
        expected_candidate_output,
    )
    assert projection["budget_exhausted_reason"] == (
        expected_task_error if expected_task_status == "WAITING_APPROVAL" else None
    )
    assert projection["run"] == (
        expected_run_status,
        None if expected_run_status == "SUCCEEDED" else expected_task_error,
        expected_run_output,
    )
    assert projection["attempt"][:2] == (
        expected_attempt_status,
        None if expected_attempt_status == "SUCCEEDED" else expected_task_error,
    )
    assert projection["attempt"][2:] == (25, 100, "CONSERVATIVE_ESTIMATE")
    assert projection["subtask"] is None
    assert projection["execution"] == (observation.phase.value, 3)
    assert len(projection["drains"]) == 1
    drain_id, drain_target, drain_reason, drain_status = projection["drains"][0]
    assert (drain_target, drain_status) == (expected_drain_target, "COMPLETE")
    assert drain_reason == (
        "coordination.runtime_reconciliation_required"
        if expected_drain_target == "RUNNING"
        else expected_task_error
    )

    assert len(projection["evidence"]) == 2
    anchor = next(value for value in projection["evidence"] if value[4] == "APPLIED")
    conclusion = next(value for value in projection["evidence"] if value[4] == "RECONCILED")
    assert anchor[:5] == (
        unknown.observation_id,
        TerminalObservationValidator.digest(unknown),
        unknown.phase.value,
        2,
        "APPLIED",
    )
    assert conclusion[:5] == (
        observation.observation_id,
        TerminalObservationValidator.digest(observation),
        observation.phase.value,
        3,
        "RECONCILED",
    )
    assert conclusion[5]["unknown_observation_id"] == unknown.observation_id
    assert conclusion[5]["unknown_observation_digest"] == anchor[1]
    assert conclusion[5].get("quarantined_output") == (
        observation.output if output_quarantined else None
    )

    assert len(projection["resolutions"]) == 1
    resolution_id, action, previous_status, resulting_status, details = projection[
        "resolutions"
    ][0]
    assert action == f"RECONCILE_RUNTIME_{observation.phase.value}"
    assert (previous_status, resulting_status) == (
        TaskStatus.RECONCILIATION_REQUIRED.value,
        expected_task_status,
    )
    assert details == {
        "target_type": "COORDINATED_RUNTIME_EXECUTION",
        "execution_id": str(execution.id),
        "run_id": str(fixture.run.id),
        "attempt_id": str(fixture.attempt.id),
        "role": "SUPERVISOR",
        "previous_phase": unknown.phase.value,
        "previous_provider_sequence": 2,
        "confirmed_phase": observation.phase.value,
        "previous_attempt_status": "OUTCOME_UNKNOWN",
        "previous_run_status": "RECONCILIATION_REQUIRED",
        "previous_subtask_status": None,
        "resulting_task_status": expected_task_status,
        "resulting_run_status": expected_run_status,
        "resulting_attempt_status": expected_attempt_status,
        "resulting_subtask_status": None,
        "effective_drain_target": expected_drain_target,
        "effective_drain_id": str(drain_id),
        "effective_drain_reason": drain_reason,
        "assignment_digest": execution.assignment_digest,
        "observation_id": observation.observation_id,
        "observation_digest": TerminalObservationValidator.digest(observation),
        "unknown_observation_digest": anchor[1],
        "scheduled_run_ids": [],
        "output_quarantined": output_quarantined,
        "cancel_intent_present": False,
        "provider_sequence": 3,
    }

    assert len(projection["events"]) == 1
    topic, envelope = projection["events"][0]
    assert topic == "agentmesh.runtime.outcome-reconciled"
    assert envelope["payload"] == {
        "tenant_id": fixture.tenant_id,
        "task_id": str(fixture.task.id),
        "run_id": str(fixture.run.id),
        "attempt_id": str(fixture.attempt.id),
        "runtime_execution_id": str(execution.id),
        "resolution_id": str(resolution_id),
        "previous_phase": unknown.phase.value,
        "confirmed_phase": observation.phase.value,
        "effective_drain_id": str(drain_id),
        "effective_drain_target": expected_drain_target,
        "effective_drain_reason": drain_reason,
        "scheduled_run_ids": [],
    }
    assert len(projection["idempotency"]) == 1
    idempotency_key, request_hash, result = projection["idempotency"][0]
    assert idempotency_key == "reconcile-1"
    assert len(request_hash) == 64
    assert result == {"resolution_id": str(resolution_id), "scheduled_run_ids": []}


@pytest.mark.parametrize(
    "phase",
    [RuntimePhase.SUCCEEDED, RuntimePhase.FAILED, RuntimePhase.CANCELED, RuntimePhase.TIMED_OUT],
)
def test_postgres_reconciliation_applies_each_known_terminal_once(phase):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _parked(engine)
    scheduler = _RecordingScheduler()
    service = _service(fixture, scheduler)
    observation = _terminal(execution, now, phase)
    try:
        first = _reconcile(service, fixture, execution, observation)
        before = (_counts(engine, fixture), _projection(engine, fixture, execution))
        replay = _reconcile(service, fixture, execution, observation)
        assert replay == first
        assert (_counts(engine, fixture), _projection(engine, fixture, execution)) == before
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM runtime_observations WHERE runtime_execution_id = :id"),
                {"id": execution.id},
            ) == 2
            assert connection.scalar(
                text("SELECT count(*) FROM task_resolutions WHERE task_id = :id"),
                {"id": fixture.task.id},
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant "
                    "AND topic = 'agentmesh.runtime.outcome-reconciled'"
                ),
                {"tenant": fixture.tenant_id},
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM idempotency_records "
                    "WHERE scope = :scope"
                ),
                {"scope": _reconciliation_scope_for_fixture(fixture, execution)},
            ) == 1
        assert len(scheduler.calls) == (1 if phase is RuntimePhase.SUCCEEDED else 0)
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


def test_postgres_reconciliation_concurrent_exact_duplicate_has_one_commit():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"], pool_size=4, max_overflow=0)
    fixture, execution, now = _parked(engine)
    scheduler = _RecordingScheduler()
    service = _service(fixture, scheduler)
    observation = _terminal(execution, now, RuntimePhase.SUCCEEDED)
    try:
        def deliver(_):
            return _reconcile(service, fixture, execution, observation)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(deliver, range(2)))
        assert len({value.resolution.id for value in results}) == 1
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM task_resolutions WHERE task_id = :id"),
                {"id": fixture.task.id},
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant "
                    "AND topic = 'agentmesh.runtime.outcome-reconciled'"
                ),
                {"tenant": fixture.tenant_id},
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM idempotency_records "
                    "WHERE scope = :scope"
                ),
                {"scope": _reconciliation_scope_for_fixture(fixture, execution)},
            ) == 1
        assert len(scheduler.calls) == 1
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


def test_postgres_reconciliation_competing_conclusion_is_rejected_without_writes():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _parked(engine)
    service = _service(fixture, _RecordingScheduler())
    first_observation = _terminal(execution, now, RuntimePhase.FAILED)
    second_observation = _terminal(execution, now, RuntimePhase.SUCCEEDED)
    try:
        _reconcile(service, fixture, execution, first_observation, key="failed")
        before = _counts(engine, fixture)
        with pytest.raises(RuntimeExecutionConflict):
            _reconcile(service, fixture, execution, second_observation, key="success")
        assert _counts(engine, fixture) == before
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


def test_postgres_reconciliation_preserves_stopping_drain_and_quarantines_late_success():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _parked(
        engine, existing_drain_target=CoordinationRuntimeDrainTarget.FAILED
    )
    service = _service(fixture, _RecordingScheduler())
    observation = _terminal(execution, now, RuntimePhase.SUCCEEDED)
    try:
        _reconcile(service, fixture, execution, observation)
        projection = _projection(engine, fixture, execution)
        assert projection["task"][0] == TaskStatus.FAILED.value
        assert projection["run"][2] is None
        with engine.connect() as connection:
            evidence = connection.execute(
                text("SELECT evidence FROM runtime_observations WHERE observation_id = :id"),
                {"id": observation.observation_id},
            ).scalar_one()
            assert evidence["quarantined_output"] == observation.output
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


def test_postgres_reconciliation_budget_wait_is_atomic_and_keeps_accounting_unchanged():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _parked(engine, budget=True)
    budget = TaskBudget.create(
        max_tokens=25,
        token_reservation_per_attempt=25,
        max_cost_micros=100,
        cost_reservation_micros_per_attempt=100,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE tasks SET budget = CAST(:budget AS jsonb), "
                "settled_tokens = 26 WHERE id = :id"
            ),
            {"budget": json.dumps(budget.to_dict()), "id": fixture.task.id},
        )
    service = _service(fixture, _RecordingScheduler())
    observation = _terminal(execution, now, RuntimePhase.SUCCEEDED)
    try:
        before = _budget_projection(engine, fixture)
        _reconcile(service, fixture, execution, observation)
        after = _projection(engine, fixture, execution)
        assert after["task"][0] == TaskStatus.WAITING_APPROVAL.value
        assert after["task"][3] is None and after["task"][4] is None
        assert _budget_projection(engine, fixture) == before
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


def test_postgres_requested_cancel_converges_to_canceled_not_failure():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _parked(engine)
    intent_now = datetime.now(timezone.utc)
    with fixture.factory() as uow:
        uow.runtimes.add_lifecycle_operation(
            RuntimeLifecycleIntent(
                id=uuid4(),
                tenant_id=fixture.tenant_id,
                runtime_execution_id=execution.id,
                operation_id=f"runtime-cancel:{execution.id}:v1",
                operation=RuntimeLifecycleOperation.CANCEL,
                intent_digest="c" * 64,
                status=RuntimeLifecycleStatus.REQUESTED,
                deadline=intent_now + timedelta(minutes=10),
                receipt_summary=None,
                version=1,
                created_at=intent_now,
                updated_at=intent_now,
            )
        )
        uow.commit()
    service = _service(fixture, _RecordingScheduler())
    observation = _terminal(execution, now, RuntimePhase.CANCELED)
    try:
        _reconcile(service, fixture, execution, observation)
        projection = _projection(engine, fixture, execution)
        assert projection["task"][0] == TaskStatus.CANCELED.value
        assert projection["run"][0] == "CANCELED"
        assert projection["attempt"][0] == "CANCELED"
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


def test_postgres_supervisor_reconciliation_completes_task_and_never_schedules():
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _supervisor_fixture(engine)
    unknown = _unknown_observation(
        execution,
        phase=RuntimePhase.OUTCOME_UNKNOWN,
        observed_at=now + timedelta(seconds=1),
    )
    parked_result = _park_unknown(
        _unknown_service(fixture),
        fixture,
        execution,
        unknown,
        received_at=now + timedelta(seconds=2),
    )
    assert parked_result.kind.value in {"PARKED", "DRAINING_ACTIVE", "DRAINING_RECONCILIATION"}
    scheduler = _RecordingScheduler()
    service = _service(fixture, scheduler)
    observation = _terminal(execution, now, RuntimePhase.SUCCEEDED)
    try:
        _reconcile(service, fixture, execution, observation)
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT status FROM tasks WHERE id = :id"), {"id": fixture.task.id}
            ) == TaskStatus.COMPLETED.value
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM task_runs "
                    "WHERE task_id = :id AND role = 'SUPERVISOR'"
                ),
                {"id": fixture.task.id},
            ) == 1
        assert scheduler.calls == []
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


@pytest.mark.parametrize("unknown_phase", [RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN])
@pytest.mark.parametrize(
    (
        "terminal_phase",
        "expected_task_status",
        "expected_run_status",
        "expected_attempt_status",
        "expected_drain_target",
        "expected_error",
    ),
    [
        (RuntimePhase.SUCCEEDED, "COMPLETED", "SUCCEEDED", "SUCCEEDED", "RUNNING", None),
        (RuntimePhase.FAILED, "FAILED", "FAILED", "FAILED", "FAILED", "runtime.failed"),
        (
            RuntimePhase.CANCELED,
            "FAILED",
            "FAILED",
            "FAILED",
            "FAILED",
            "runtime.unrequested_cancellation",
        ),
        (
            RuntimePhase.TIMED_OUT,
            "FAILED",
            "FAILED",
            "FAILED",
            "FAILED",
            "runtime.timed_out",
        ),
    ],
)
def test_postgres_supervisor_reconciliation_qualifies_unknown_and_terminal_matrix(
    unknown_phase,
    terminal_phase,
    expected_task_status,
    expected_run_status,
    expected_attempt_status,
    expected_drain_target,
    expected_error,
):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now, unknown = _parked_supervisor(
        engine, unknown_phase=unknown_phase
    )
    scheduler = _RecordingScheduler()
    service = _service(fixture, scheduler)
    observation = _terminal(execution, now, terminal_phase)
    before_budget = _budget_projection(engine, fixture)
    try:
        _reconcile(service, fixture, execution, observation)
        projection = _qualification_projection(engine, fixture, execution)
        _assert_reconciliation_records(
            projection,
            fixture,
            execution,
            unknown,
            observation,
            expected_task_status=expected_task_status,
            expected_run_status=expected_run_status,
            expected_attempt_status=expected_attempt_status,
            expected_drain_target=expected_drain_target,
            expected_task_error=expected_error,
            expected_task_output=(
                observation.output if terminal_phase is RuntimePhase.SUCCEEDED else None
            ),
            expected_run_output=(
                observation.output if terminal_phase is RuntimePhase.SUCCEEDED else None
            ),
        )
        assert projection["budget"] == before_budget
        assert projection["budget"][:2] == (
            (0, 25, 0, 100),
            (25, 25, 100, 100, "CONSERVATIVE_ESTIMATE"),
        )
        assert len(projection["budget"][2]) == 1
        assert projection["budget"][2][0][0] is not None
        assert scheduler.calls == []
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


@pytest.mark.parametrize("unknown_phase", [RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN])
def test_postgres_supervisor_success_waits_on_expired_budget_without_accounting_drift(
    unknown_phase,
):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now, unknown = _parked_supervisor(
        engine, unknown_phase=unknown_phase
    )
    _set_expired_budget(engine, fixture, deadline=now + timedelta(seconds=1))
    scheduler = _RecordingScheduler()
    observation = _terminal(execution, now, RuntimePhase.SUCCEEDED)
    before_budget = _budget_projection(engine, fixture)
    try:
        _reconcile(_service(fixture, scheduler), fixture, execution, observation)
        projection = _qualification_projection(engine, fixture, execution)
        _assert_reconciliation_records(
            projection,
            fixture,
            execution,
            unknown,
            observation,
            expected_task_status="WAITING_APPROVAL",
            expected_run_status="SUCCEEDED",
            expected_attempt_status="SUCCEEDED",
            expected_drain_target="WAITING_APPROVAL",
            expected_task_error="budget_deadline_exceeded",
            expected_candidate_output=observation.output,
            expected_run_output=observation.output,
        )
        assert projection["budget"] == before_budget
        assert projection["budget"][:2] == (
            (0, 25, 0, 100),
            (25, 25, 100, 100, "CONSERVATIVE_ESTIMATE"),
        )
        assert len(projection["budget"][2]) == 1
        assert projection["budget"][2][0][0] is not None
        assert scheduler.calls == []
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


@pytest.mark.parametrize("unknown_phase", [RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN])
def test_postgres_supervisor_late_success_under_wait_is_quarantined(unknown_phase):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now, unknown = _parked_supervisor(
        engine,
        unknown_phase=unknown_phase,
        existing_drain_target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
    )
    scheduler = _RecordingScheduler()
    observation = _terminal(execution, now, RuntimePhase.SUCCEEDED)
    before_budget = _budget_projection(engine, fixture)
    try:
        _reconcile(_service(fixture, scheduler), fixture, execution, observation)
        projection = _qualification_projection(engine, fixture, execution)
        _assert_reconciliation_records(
            projection,
            fixture,
            execution,
            unknown,
            observation,
            expected_task_status="WAITING_APPROVAL",
            expected_run_status="SUCCEEDED",
            expected_attempt_status="SUCCEEDED",
            expected_drain_target="WAITING_APPROVAL",
            expected_task_error="existing first cause",
            output_quarantined=True,
        )
        assert projection["budget"] == before_budget
        assert projection["budget"][:2] == (
            (0, 25, 0, 100),
            (25, 25, 100, 100, "CONSERVATIVE_ESTIMATE"),
        )
        assert len(projection["budget"][2]) == 1
        assert projection["budget"][2][0][0] is not None
        assert scheduler.calls == []
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


@pytest.mark.parametrize("sibling_state", ["queued", "no-execution", "prepared"])
def test_postgres_reconciliation_failure_safely_releases_nonrunning_siblings(sibling_state):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _parked(engine, sibling_state=sibling_state)
    service = _service(fixture, _RecordingScheduler())
    observation = _terminal(execution, now, RuntimePhase.FAILED)
    try:
        _reconcile(service, fixture, execution, observation)
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT status FROM tasks WHERE id = :id"), {"id": fixture.task.id}
            ) == TaskStatus.FAILED.value
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM coordination_runtime_drains "
                    "WHERE task_id = :id AND status = 'COMPLETE'"
                ),
                {"id": fixture.task.id},
            ) == 1
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()


def test_postgres_reconciliation_execution_writer_failure_rolls_back_every_projection(monkeypatch):
    engine = create_engine(os.environ["AGENTMESH_DATABASE_URL"])
    fixture, execution, now = _parked(engine)
    service = _service(fixture, _RecordingScheduler())
    observation = _terminal(execution, now, RuntimePhase.SUCCEEDED)
    before = (_counts(engine, fixture), _projection(engine, fixture, execution))
    # Evidence/transition rollback is already qualified by the c2e2 suite; this
    # test keeps the c2e3 transaction boundary explicit for its final writers.
    try:
        def fail(*args, **kwargs):
            raise RuntimeError("injected writer failure")

        monkeypatch.setattr(SqlAlchemyRuntimeRepository, "save_execution", fail)
        with pytest.raises(RuntimeError, match="injected writer failure"):
            _reconcile(service, fixture, execution, observation)
        assert (_counts(engine, fixture), _projection(engine, fixture, execution)) == before
    finally:
        _cleanup_reconciliation(engine, fixture, execution)
        engine.dispose()
