from __future__ import annotations

import ast
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_reconciliation import (
    CoordinatedRuntimeReconciliationService,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedRuntimeUnknownOutcomeService,
)
from agentmesh.application.runtime_contracts import TerminalObservationValidator
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    AuthorizationDenied,
    FeatureDisabled,
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeObservationOutcome,
)
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskStatus
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import (
    ErrorCategory,
    RetryDisposition,
    RuntimeErrorDTO,
    RuntimeObservation,
    RuntimePhase,
)
from tests.test_coordinated_runtime_unknown_planner import _cancel_intent
from tests.test_coordinated_runtime_unknown_service import (
    _Locker,
    _real_snapshot_aggregate,
    _real_stopping_sibling_aggregate,
    _real_supervisor_snapshot_aggregate,
    _Uow,
)
from tests.test_coordinated_runtime_unknown_service import (
    _observation as _unknown_observation,
)


class _DynamicLocker:
    def __init__(self) -> None:
        self.calls = 0

    def lock(self, uow, *, tenant_id, task_id):
        self.calls += 1
        if hasattr(uow, "operation_log"):
            uow.operation_log.append("aggregate_lock")
        return uow.aggregate


class _Scheduler:
    def __init__(self) -> None:
        self.calls = 0

    def schedule(self, uow, task, *, at, causation_id):
        self.calls += 1
        return []


class _Memory:
    def __init__(self) -> None:
        self.tasks = []

    def capture_completed_task_in_unit_of_work(self, uow, task):
        self.tasks.append(task.id)


class _TransactionalUow(_Uow):
    def _write(self, name):
        self._maybe_fail(name)
        self.write_counts[name] = self.write_counts.get(name, 0) + 1

    def _drain_get(self, drain_id, *, tenant_id, for_update=False):
        candidates = (self.drain, self.aggregate.active_drain)
        return next(
            (
                value
                for value in candidates
                if value is not None
                and value.id == drain_id
                and value.tenant_id == tenant_id
            ),
            None,
        )

    def __enter__(self):
        super().__enter__()
        self._reconciliation_snapshot = (
            dict(self.idempotency_values),
            dict(self.resolution_values),
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        if exc_type is not None:
            idempotency, resolutions = self._reconciliation_snapshot
            self.idempotency_values.clear()
            self.idempotency_values.update(idempotency)
            self.resolution_values.clear()
            self.resolution_values.update(resolutions)
        return result


def _principal(*, tenant_id="tenant-a", role=Role.OPERATOR, authenticated=True):
    return PrincipalContext(
        principal_id="coordinated-operator",
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({role}),
        authenticated=authenticated,
        authentication_method="test",
    )


def _augment_uow(uow):
    uow.write_counts = {}
    uow.idempotency_values = {}
    uow.resolution_values = {}
    def add_idempotency(value):
        uow._maybe_fail("idempotency_add")
        uow.idempotency_values[(value.scope, value.key)] = value

    def add_resolution(value):
        uow._maybe_fail("resolution_add")
        uow.resolution_values[value.id] = value

    def lock_idempotency(scope, key):
        if hasattr(uow, "operation_log"):
            uow.operation_log.append("idempotency_lock")

    uow.idempotency = SimpleNamespace(
        lock=lock_idempotency,
        get=lambda scope, key: uow.idempotency_values.get((scope, key)),
        add=add_idempotency,
    )
    uow.task_resolutions = SimpleNamespace(
        add=add_resolution,
        get=lambda resolution_id: uow.resolution_values.get(resolution_id),
    )
    uow.runtimes.prior_observations = (
        lambda execution_id, tenant_id, observation_id, digest: [
            value
            for value in uow.observations
            if value.runtime_execution_id == execution_id
            and value.tenant_id == tenant_id
            and (
                value.observation_id == observation_id
                or value.observation_digest == digest
            )
        ]
    )
    return uow


def _parked_case(
    *,
    supervisor=False,
    unknown_phase=RuntimePhase.OUTCOME_UNKNOWN,
    drain_target=None,
):
    if supervisor:
        task, target, aggregate = _real_supervisor_snapshot_aggregate()
        if drain_target is not None:
            aggregate = replace(
                aggregate,
                active_drain=CoordinationRuntimeDrain.start(
                    drain_id=uuid4(),
                    tenant_id=task.tenant_id,
                    task_id=task.id,
                    triggering_run_id=target[1].id,
                    target=drain_target,
                    reason="first cause",
                    at=target[3].updated_at,
                ),
            )
    elif drain_target is not None:
        task, target, aggregate = _real_stopping_sibling_aggregate(drain_target)
    else:
        task, target, aggregate = _real_snapshot_aggregate()
    uow = _augment_uow(_TransactionalUow(aggregate))
    now = target[3].updated_at + timedelta(seconds=2)
    unknown = _unknown_observation(target, now, phase=unknown_phase)
    CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=_Locker(aggregate),
    ).park_unknown(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=target[1].id,
        attempt_id=target[2].id,
        fencing_token=target[2].fencing_token,
        runtime_execution_id=target[3].id,
        observation=unknown,
        received_at=now,
        causation_id=uuid4(),
    )
    uow.aggregate = replace(
        uow.aggregate,
        active_drain=uow.drain or aggregate.active_drain,
        boundary_classifications={
            **dict(uow.aggregate.boundary_classifications),
            target[1].id: CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,
        },
    )
    target = (
        next(value for value in uow.aggregate.subtasks if value.id == target[0].id),
        next(value for value in uow.aggregate.runs if value.id == target[1].id),
        uow.aggregate.latest_attempts[target[1].id],
        next(value for value in uow.aggregate.executions if value.id == target[3].id),
    )
    uow.write_counts.clear()
    return task, target, uow, now + timedelta(seconds=1)


def _terminal(target, now, phase):
    execution = target[3]
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=now,
        provider_event_id="independent-terminal-evidence",
        provider_sequence=3,
        output={"answer": 42} if phase is RuntimePhase.SUCCEEDED else None,
        error=(
            RuntimeErrorDTO(
                code="provider.failed",
                category=ErrorCategory.PERMANENT,
                message="safe failure",
                retry_disposition=RetryDisposition.NEVER,
            )
            if phase in {RuntimePhase.FAILED, RuntimePhase.TIMED_OUT}
            else None
        ),
    )


def _service(uow, scheduler, locker, *, memory=None):
    return CoordinatedRuntimeReconciliationService(
        uow_factory=lambda: uow,
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
        ),
        coordinated_scheduler=scheduler,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
        runtime_memory_service=memory,
    )


def _command(task, target, observation, at, **changes):
    values = {
        "tenant_id": task.tenant_id,
        "task_id": task.id,
        "run_id": target[1].id,
        "attempt_id": target[2].id,
        "fencing_token": target[2].fencing_token,
        "runtime_execution_id": target[3].id,
        "principal": _principal(),
        "observation": observation,
        "evidence_digest": TerminalObservationValidator.digest(observation),
        "evidence_reference": "audit://independent/terminal-proof",
        "reason": "independently verified provider conclusion",
        "idempotency_key": "coordinated-reconcile-1",
        "received_at": at,
    }
    values.update(changes)
    return values


@pytest.mark.parametrize(
    ("phase", "expected_run", "expected_task", "scheduled"),
    [
        (RuntimePhase.SUCCEEDED, RunStatus.SUCCEEDED, TaskStatus.RUNNING, 1),
        (RuntimePhase.FAILED, RunStatus.FAILED, TaskStatus.FAILED, 0),
        (RuntimePhase.CANCELED, RunStatus.FAILED, TaskStatus.FAILED, 0),
        (RuntimePhase.TIMED_OUT, RunStatus.FAILED, TaskStatus.FAILED, 0),
    ],
)
def test_executor_reconciliation_applies_four_conclusions(
    phase, expected_run, expected_task, scheduled
) -> None:
    task, target, uow, at = _parked_case()
    observation = _terminal(target, at, phase)
    scheduler, locker = _Scheduler(), _DynamicLocker()
    result = _service(uow, scheduler, locker).reconcile_known_terminal(
        **_command(task, target, observation, at)
    )
    assert locker.calls == 1 and uow.commits == 2
    assert result.execution.phase is RuntimeExecutionPhase(phase.value)
    assert target[1].status is expected_run
    assert target[2].status is (
        AttemptStatus.SUCCEEDED if phase is RuntimePhase.SUCCEEDED else AttemptStatus.FAILED
    )
    assert task.status is expected_task
    assert scheduler.calls == scheduled
    assert uow.write_counts.get("task_save") == 1
    assert len(uow.resolution_values) == 1
    assert [value.schema_name for value in uow.outbox_values].count(
        "agentmesh.runtime.outcome-reconciled"
    ) == 1


def test_exact_idempotency_replay_has_no_second_business_writes() -> None:
    task, target, uow, at = _parked_case()
    observation = _terminal(target, at, RuntimePhase.SUCCEEDED)
    scheduler, locker = _Scheduler(), _DynamicLocker()
    service = _service(uow, scheduler, locker)
    command = _command(task, target, observation, at)
    first = service.reconcile_known_terminal(**command)
    uow.aggregate = replace(uow.aggregate, active_drain=uow.drain)
    before = (
        len(uow.observations),
        len(uow.outbox_values),
        len(uow.resolution_values),
        scheduler.calls,
        uow.commits,
    )
    replay = service.reconcile_known_terminal(**command)
    assert replay == first
    assert (
        len(uow.observations),
        len(uow.outbox_values),
        len(uow.resolution_values),
        scheduler.calls,
        uow.commits,
    ) == before
    with pytest.raises(IdempotencyConflict):
        service.reconcile_known_terminal(
            **_command(task, target, observation, at, reason="changed conclusion")
        )


def test_aggregate_lock_precedes_same_uow_idempotency_lock() -> None:
    task, target, uow, at = _parked_case()
    uow.operation_log = []
    observation = _terminal(target, at, RuntimePhase.FAILED)
    _service(uow, _Scheduler(), _DynamicLocker()).reconcile_known_terminal(
        **_command(task, target, observation, at)
    )
    assert uow.operation_log[:2] == ["aggregate_lock", "idempotency_lock"]


@pytest.mark.parametrize(
    ("phase", "expected_task"),
    [
        (RuntimePhase.SUCCEEDED, TaskStatus.COMPLETED),
        (RuntimePhase.FAILED, TaskStatus.FAILED),
        (RuntimePhase.CANCELED, TaskStatus.FAILED),
        (RuntimePhase.TIMED_OUT, TaskStatus.FAILED),
    ],
)
@pytest.mark.parametrize("unknown_phase", [RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN])
def test_supervisor_reconciliation_supports_both_unknowns_and_four_conclusions(
    phase, expected_task, unknown_phase
) -> None:
    task, target, uow, at = _parked_case(
        supervisor=True, unknown_phase=unknown_phase
    )
    observation = _terminal(target, at, phase)
    scheduler, locker = _Scheduler(), _DynamicLocker()
    result = _service(uow, scheduler, locker).reconcile_known_terminal(
        **_command(task, target, observation, at)
    )
    assert result.execution.phase is RuntimeExecutionPhase(phase.value)
    assert task.status is expected_task
    assert scheduler.calls == 0
    if phase is RuntimePhase.SUCCEEDED:
        assert task.output == {"answer": 42}


@pytest.mark.parametrize(
    "drain_target",
    [
        CoordinationRuntimeDrainTarget.FAILED,
        CoordinationRuntimeDrainTarget.CANCELED,
        CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
    ],
)
def test_late_success_under_stopping_drain_is_quarantined(drain_target) -> None:
    task, target, uow, at = _parked_case(drain_target=drain_target)
    observation = _terminal(target, at, RuntimePhase.SUCCEEDED)
    scheduler, locker = _Scheduler(), _DynamicLocker()
    _service(uow, scheduler, locker).reconcile_known_terminal(
        **_command(task, target, observation, at)
    )
    assert target[1].status is RunStatus.SUCCEEDED and target[1].output is None
    assert target[0].status.value == "COMPLETED" and target[0].output is None
    assert task.output is None and task.candidate_output is None
    assert scheduler.calls == 0
    conclusion = uow.observations[-1]
    assert conclusion.evidence["quarantined_output"] == {"answer": 42}


def test_supervisor_success_uses_precomputed_budget_wait_without_scheduling() -> None:
    task, target, uow, at = _parked_case(supervisor=True)
    task.budget = TaskBudget.create(deadline=at)
    target[2].budget_settlement_source = BudgetSettlementSource.CONSERVATIVE_ESTIMATE
    target[2].settled_tokens = target[2].reserved_tokens
    target[2].settled_cost_micros = target[2].reserved_cost_micros
    observation = _terminal(target, at, RuntimePhase.SUCCEEDED)
    scheduler, locker = _Scheduler(), _DynamicLocker()
    _service(uow, scheduler, locker).reconcile_known_terminal(
        **_command(task, target, observation, at)
    )
    assert task.status is TaskStatus.WAITING_APPROVAL
    assert task.candidate_output == {"answer": 42}
    assert task.error == "budget_deadline_exceeded"
    assert scheduler.calls == 0


def test_supervisor_late_success_under_existing_wait_is_quarantined() -> None:
    task, target, uow, at = _parked_case(
        supervisor=True,
        drain_target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
    )
    observation = _terminal(target, at, RuntimePhase.SUCCEEDED)
    memory = _Memory()
    _service(
        uow, _Scheduler(), _DynamicLocker(), memory=memory
    ).reconcile_known_terminal(**_command(task, target, observation, at))
    assert target[1].status is RunStatus.SUCCEEDED and target[1].output is None
    assert task.status is TaskStatus.WAITING_APPROVAL
    assert task.output is None and task.candidate_output is None
    assert task.error == "first cause"
    assert memory.tasks == []
    assert uow.observations[-1].evidence["quarantined_output"] == {"answer": 42}


def test_requested_cancellation_keeps_canceled_business_semantics() -> None:
    task, target, uow, at = _parked_case()
    intent = _cancel_intent(
        tenant_id=task.tenant_id,
        execution_id=target[3].id,
        operation_id=f"runtime-cancel:{target[3].id}:v1",
    )
    uow.aggregate = replace(uow.aggregate, lifecycle_operations=(intent,))
    observation = _terminal(target, at, RuntimePhase.CANCELED)
    scheduler, locker = _Scheduler(), _DynamicLocker()
    _service(uow, scheduler, locker).reconcile_known_terminal(
        **_command(task, target, observation, at)
    )
    assert target[2].status is AttemptStatus.CANCELED
    assert target[1].status is RunStatus.CANCELED
    assert target[0].status is SubtaskStatus.CANCELED
    assert task.status is TaskStatus.CANCELED
    assert scheduler.calls == 0


def test_executor_success_budget_wait_never_schedules() -> None:
    task, target, uow, at = _parked_case()
    task.budget = TaskBudget.create(deadline=at)
    target[2].budget_settlement_source = BudgetSettlementSource.CONSERVATIVE_ESTIMATE
    target[2].settled_tokens = target[2].reserved_tokens
    target[2].settled_cost_micros = target[2].reserved_cost_micros
    observation = _terminal(target, at, RuntimePhase.SUCCEEDED)
    scheduler, locker = _Scheduler(), _DynamicLocker()
    _service(uow, scheduler, locker).reconcile_known_terminal(
        **_command(task, target, observation, at)
    )
    assert task.status is TaskStatus.WAITING_APPROVAL
    assert task.output is None and task.candidate_output is None
    assert scheduler.calls == 0


def test_completion_memory_is_supervisor_completed_only() -> None:
    memory = _Memory()
    task, target, uow, at = _parked_case()
    observation = _terminal(target, at, RuntimePhase.SUCCEEDED)
    _service(uow, _Scheduler(), _DynamicLocker(), memory=memory).reconcile_known_terminal(
        **_command(task, target, observation, at)
    )
    assert memory.tasks == []

    task, target, uow, at = _parked_case(supervisor=True)
    observation = _terminal(target, at, RuntimePhase.SUCCEEDED)
    _service(uow, _Scheduler(), _DynamicLocker(), memory=memory).reconcile_known_terminal(
        **_command(task, target, observation, at)
    )
    assert memory.tasks == [task.id]


@pytest.mark.parametrize(
    "mutation",
    [
        "task_status",
        "task_error",
        "run_status",
        "attempt_status",
        "execution_phase",
        "boundary",
        "subtask_status",
        "drain",
        "evidence_missing",
        "evidence_ambiguous",
        "assignment_digest",
        "quota_active",
    ],
)
def test_partial_parked_projection_fails_before_business_writes(mutation) -> None:
    task, target, uow, at = _parked_case()
    if mutation == "task_status":
        task.status = TaskStatus.RUNNING
    elif mutation == "task_error":
        task.error = "different"
    elif mutation == "run_status":
        target[1].status = RunStatus.RUNNING
    elif mutation == "attempt_status":
        target[2].status = AttemptStatus.RUNNING
    elif mutation == "execution_phase":
        changed = replace(target[3], phase=RuntimeExecutionPhase.RUNNING)
        uow.aggregate = replace(uow.aggregate, executions=(changed,))
        target = (target[0], target[1], target[2], changed)
    elif mutation == "boundary":
        uow.aggregate = replace(
            uow.aggregate,
            boundary_classifications={
                target[1].id: CoordinationRuntimeBoundary.CROSSED_ACTIVE
            },
        )
    elif mutation == "subtask_status":
        target[0].status = SubtaskStatus.RUNNING
    elif mutation == "drain":
        uow.aggregate = replace(uow.aggregate, active_drain=None)
    elif mutation == "evidence_missing":
        uow.observations.clear()
    elif mutation == "evidence_ambiguous":
        uow.observations.append(replace(uow.observations[0], id=uuid4()))
    elif mutation == "assignment_digest":
        changed = replace(target[3], assignment_digest="f" * 64)
        uow.aggregate = replace(uow.aggregate, executions=(changed,))
        target = (target[0], target[1], target[2], changed)
    elif mutation == "quota_active":
        uow.reservations.append(SimpleNamespace(released_at=None))
    observation = _terminal(target, at, RuntimePhase.FAILED)
    before = (
        len(uow.observations),
        len(uow.outbox_values),
        len(uow.resolution_values),
        len(uow.idempotency_values),
        uow.commits,
    )
    with pytest.raises((RuntimeExecutionConflict, InvalidTaskTransition)):
        _service(uow, _Scheduler(), _DynamicLocker()).reconcile_known_terminal(
            **_command(task, target, observation, at)
        )
    assert (
        len(uow.observations),
        len(uow.outbox_values),
        len(uow.resolution_values),
        len(uow.idempotency_values),
        uow.commits,
    ) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "resolution_missing",
        "resolution_reason",
        "resolution_details",
        "resolution_previous_phase",
        "resolution_previous_sequence",
        "result_non_mapping",
        "result_resolution_uuid",
        "result_scheduled_type",
        "result_scheduled_uuid",
        "result_extra_key",
        "event_missing",
        "event_payload",
        "evidence_missing",
        "evidence_reference",
        "evidence_sequence",
        "anchor_tenant",
        "anchor_execution",
        "anchor_assignment",
        "anchor_processing",
        "anchor_summary",
        "anchor_sequence",
        "anchor_evidence_phase",
        "anchor_evidence_reason",
        "drain_missing",
        "execution_phase",
        "run_status",
        "attempt_status",
        "subtask_status",
        "task_status",
        "task_error",
        "task_candidate",
        "run_output",
        "quota_active",
        "assignment_digest",
    ],
)
def test_replay_validates_full_persisted_projection_before_return(mutation) -> None:
    task, target, uow, at = _parked_case()
    observation = _terminal(target, at, RuntimePhase.SUCCEEDED)
    scheduler, locker = _Scheduler(), _DynamicLocker()
    service = _service(uow, scheduler, locker)
    command = _command(task, target, observation, at)
    service.reconcile_known_terminal(**command)
    uow.aggregate = replace(uow.aggregate, active_drain=uow.drain)
    if mutation == "resolution_missing":
        uow.resolution_values.clear()
    elif mutation == "resolution_reason":
        resolution_id = next(iter(uow.resolution_values))
        uow.resolution_values[resolution_id] = replace(
            uow.resolution_values[resolution_id], reason="tampered"
        )
    elif mutation == "resolution_details":
        resolution_id = next(iter(uow.resolution_values))
        resolution = uow.resolution_values[resolution_id]
        uow.resolution_values[resolution_id] = replace(
            resolution,
            details={**resolution.details, "provider_sequence": 999},
        )
    elif mutation == "resolution_previous_phase":
        resolution_id = next(iter(uow.resolution_values))
        resolution = uow.resolution_values[resolution_id]
        uow.resolution_values[resolution_id] = replace(
            resolution,
            details={
                **resolution.details,
                "previous_phase": RuntimeExecutionPhase.LOST.value,
            },
        )
    elif mutation == "resolution_previous_sequence":
        resolution_id = next(iter(uow.resolution_values))
        resolution = uow.resolution_values[resolution_id]
        uow.resolution_values[resolution_id] = replace(
            resolution,
            details={**resolution.details, "previous_provider_sequence": 999},
        )
    elif mutation == "result_non_mapping":
        record_key = next(iter(uow.idempotency_values))
        record = uow.idempotency_values[record_key]
        uow.idempotency_values[record_key] = replace(record, result=None)
    elif mutation in {
        "result_resolution_uuid",
        "result_scheduled_type",
        "result_scheduled_uuid",
        "result_extra_key",
    }:
        record_key = next(iter(uow.idempotency_values))
        record = uow.idempotency_values[record_key]
        result = dict(record.result)
        if mutation == "result_resolution_uuid":
            result["resolution_id"] = "not-a-uuid"
        elif mutation == "result_scheduled_type":
            result["scheduled_run_ids"] = "not-a-list"
        elif mutation == "result_scheduled_uuid":
            result["scheduled_run_ids"] = ["not-a-uuid"]
        else:
            result["unexpected"] = True
        uow.idempotency_values[record_key] = replace(record, result=result)
    elif mutation == "event_missing":
        uow.outbox_values[:] = [
            value
            for value in uow.outbox_values
            if value.schema_name != "agentmesh.runtime.outcome-reconciled"
        ]
    elif mutation == "event_payload":
        index = next(
            index
            for index, value in enumerate(uow.outbox_values)
            if value.schema_name == "agentmesh.runtime.outcome-reconciled"
        )
        event = uow.outbox_values[index]
        uow.outbox_values[index] = replace(event, payload={**event.payload, "run_id": str(uuid4())})
    elif mutation == "evidence_missing":
        uow.observations[:] = [
            value
            for value in uow.observations
            if value.processing_outcome.value != "RECONCILED"
        ]
    elif mutation == "evidence_reference":
        index = next(
            index
            for index, value in enumerate(uow.observations)
            if value.processing_outcome.value == "RECONCILED"
        )
        evidence = uow.observations[index]
        uow.observations[index] = replace(
            evidence,
            evidence=MappingProxyType(
                {**dict(evidence.evidence), "evidence_reference": "audit://tampered"}
            ),
        )
    elif mutation == "evidence_sequence":
        index = next(
            index
            for index, value in enumerate(uow.observations)
            if value.processing_outcome.value == "RECONCILED"
        )
        uow.observations[index] = replace(
            uow.observations[index], provider_sequence=999
        )
    elif mutation.startswith("anchor_"):
        index = next(
            index
            for index, value in enumerate(uow.observations)
            if value.processing_outcome is RuntimeObservationOutcome.APPLIED
        )
        anchor = uow.observations[index]
        if mutation == "anchor_tenant":
            changed = replace(anchor, tenant_id="tenant-tampered")
        elif mutation == "anchor_execution":
            changed = replace(anchor, runtime_execution_id=uuid4())
        elif mutation == "anchor_assignment":
            changed = replace(anchor, assignment_id=uuid4())
        elif mutation == "anchor_processing":
            changed = replace(anchor, processing_outcome=RuntimeObservationOutcome.CONFLICT)
        elif mutation == "anchor_summary":
            changed = replace(anchor, safe_summary="tampered")
        elif mutation == "anchor_sequence":
            changed = replace(anchor, provider_sequence=999)
        elif mutation == "anchor_evidence_phase":
            changed = replace(
                anchor,
                evidence=MappingProxyType(
                    {**dict(anchor.evidence), "phase": RuntimeExecutionPhase.LOST.value}
                ),
            )
        else:
            changed = replace(
                anchor,
                evidence=MappingProxyType(
                    {**dict(anchor.evidence), "reason": "runtime.lost"}
                ),
            )
        uow.observations[index] = changed
    elif mutation == "drain_missing":
        uow.drain = None
        uow.aggregate = replace(uow.aggregate, active_drain=None)
    elif mutation == "execution_phase":
        changed = replace(target[3], phase=RuntimeExecutionPhase.OUTCOME_UNKNOWN)
        uow.aggregate = replace(uow.aggregate, executions=(changed,))
    elif mutation == "run_status":
        target[1].status = RunStatus.RECONCILIATION_REQUIRED
    elif mutation == "attempt_status":
        target[2].status = AttemptStatus.OUTCOME_UNKNOWN
    elif mutation == "subtask_status":
        target[0].status = SubtaskStatus.RECONCILIATION_REQUIRED
    elif mutation == "task_status":
        task.status = TaskStatus.RECONCILIATION_REQUIRED
    elif mutation == "task_error":
        task.error = "tampered"
    elif mutation == "task_candidate":
        task.candidate_output = {"tampered": True}
    elif mutation == "run_output":
        target[1].output = {"tampered": True}
    elif mutation == "quota_active":
        uow.reservations.append(SimpleNamespace(released_at=None))
    elif mutation == "assignment_digest":
        changed = replace(target[3], assignment_digest="f" * 64)
        uow.aggregate = replace(
            uow.aggregate,
            executions=(changed,),
        )
    before = (
        len(uow.observations),
        len(uow.outbox_values),
        len(uow.resolution_values),
        len(uow.idempotency_values),
        scheduler.calls,
        uow.commits,
    )
    with pytest.raises((RuntimeExecutionConflict, InvalidTaskTransition)):
        service.reconcile_known_terminal(**command)
    assert (
        len(uow.observations),
        len(uow.outbox_values),
        len(uow.resolution_values),
        len(uow.idempotency_values),
        scheduler.calls,
        uow.commits,
    ) == before


@pytest.mark.parametrize("projection", ["run", "subtask"])
def test_failed_replay_rejects_nonempty_terminal_output(projection) -> None:
    task, target, uow, at = _parked_case()
    observation = _terminal(target, at, RuntimePhase.FAILED)
    service = _service(uow, _Scheduler(), _DynamicLocker())
    command = _command(task, target, observation, at)
    service.reconcile_known_terminal(**command)
    uow.aggregate = replace(uow.aggregate, active_drain=uow.drain)
    if projection == "run":
        target[1].output = {"tampered": True}
    else:
        target[0].output = {"tampered": True}

    with pytest.raises(RuntimeExecutionConflict):
        service.reconcile_known_terminal(**command)


@pytest.mark.parametrize(
    "fail_at",
    [
        "evidence_add",
        "execution_save",
        "attempt_save",
        "run_save",
        "subtask_save",
        "drain_save",
        "task_save",
        "resolution_add",
        "outbox_add",
        "idempotency_add",
        "commit",
    ],
)
def test_every_reconciliation_writer_failure_rolls_back_one_uow(fail_at) -> None:
    task, target, uow, at = _parked_case()
    observation = _terminal(target, at, RuntimePhase.FAILED)
    before = (
        task.status,
        target[0].status,
        target[1].status,
        target[2].status,
        target[3].phase,
        len(uow.observations),
        len(uow.outbox_values),
        len(uow.resolution_values),
        len(uow.idempotency_values),
        uow.commits,
    )
    uow.fail_at = fail_at
    with pytest.raises(RuntimeError, match="fake writer failure"):
        _service(uow, _Scheduler(), _DynamicLocker()).reconcile_known_terminal(
            **_command(task, target, observation, at)
        )
    rolled = uow.aggregate
    rolled_run = next(value for value in rolled.runs if value.id == target[1].id)
    rolled_subtask = next(value for value in rolled.subtasks if value.id == target[0].id)
    rolled_attempt = rolled.latest_attempts[target[1].id]
    rolled_execution = next(
        value for value in rolled.executions if value.id == target[3].id
    )
    assert (
        rolled.task.status,
        rolled_subtask.status,
        rolled_run.status,
        rolled_attempt.status,
        rolled_execution.phase,
        len(uow.observations),
        len(uow.outbox_values),
        len(uow.resolution_values),
        len(uow.idempotency_values),
        uow.commits,
    ) == before


@pytest.mark.parametrize(
    "principal",
    [
        _principal(tenant_id="tenant-b"),
        _principal(authenticated=False),
        _principal(role=Role.AUDITOR),
    ],
)
def test_principal_is_rechecked_before_uow(principal) -> None:
    task, target, _uow, at = _parked_case()
    observation = _terminal(target, at, RuntimePhase.FAILED)
    with pytest.raises(AuthorizationDenied):
        CoordinatedRuntimeReconciliationService(
            uow_factory=lambda: (_ for _ in ()).throw(AssertionError("opened UoW")),
            feature_gates=FeatureGateSet.from_config(
                "full",
                "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
            ),
            coordinated_scheduler=_Scheduler(),
            cancel_deadline_window=timedelta(minutes=5),
        ).reconcile_known_terminal(
            **_command(task, target, observation, at, principal=principal)
        )


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("evidence_digest", "f" * 64),
        ("evidence_reference", " "),
        ("reason", ""),
        ("idempotency_key", ""),
    ],
)
def test_malformed_request_is_rejected_before_uow(change, value) -> None:
    task, target, _uow, at = _parked_case()
    observation = _terminal(target, at, RuntimePhase.FAILED)
    command = _command(task, target, observation, at)
    command[change] = value
    service = CoordinatedRuntimeReconciliationService(
        uow_factory=lambda: (_ for _ in ()).throw(AssertionError("opened UoW")),
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
        ),
        coordinated_scheduler=_Scheduler(),
        cancel_deadline_window=timedelta(minutes=5),
    )
    with pytest.raises((InvalidTaskInput, IdempotencyConflict)):
        service.reconcile_known_terminal(**command)


def test_feature_capabilities_are_rechecked_before_uow() -> None:
    task, target, _uow, at = _parked_case()
    observation = _terminal(target, at, RuntimePhase.FAILED)
    service = CoordinatedRuntimeReconciliationService(
        uow_factory=lambda: (_ for _ in ()).throw(AssertionError("opened UoW")),
        feature_gates=FeatureGateSet.from_config("minimal"),
        coordinated_scheduler=_Scheduler(),
        cancel_deadline_window=timedelta(minutes=5),
    )
    with pytest.raises(FeatureDisabled):
        service.reconcile_known_terminal(**_command(task, target, observation, at))


def test_existing_terminal_anchor_and_sequence_regression_fail_before_writes() -> None:
    for mutation in ("accepted", "sequence"):
        task, target, uow, at = _parked_case()
        observation = _terminal(target, at, RuntimePhase.SUCCEEDED)
        if mutation == "accepted":
            uow.terminal_phases.add(RuntimeExecutionPhase.FAILED)
        else:
            object.__setattr__(observation, "provider_sequence", 1)
        command = _command(task, target, observation, at)
        before = (len(uow.observations), len(uow.outbox_values), uow.commits)
        with pytest.raises((RuntimeExecutionConflict, InvalidTaskTransition)):
            _service(uow, _Scheduler(), _DynamicLocker()).reconcile_known_terminal(
                **command
            )
        assert (len(uow.observations), len(uow.outbox_values), uow.commits) == before


def test_c2e3_service_is_caller_free_and_has_one_uow_commit() -> None:
    root = Path(__file__).parents[1] / "src" / "agentmesh"
    path = root / "application" / "coordinated_runtime_reconciliation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
        for node in ast.walk(tree)
    ) == 1
    assert sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_uow_factory"
        for node in ast.walk(tree)
    ) == 1
    forbidden = {"adapter", "research"}
    assert not any(
        isinstance(node, ast.ImportFrom)
        and any(token in (node.module or "").lower().split(".") for token in forbidden)
        for node in ast.walk(tree)
    )
    callers = []
    for candidate in root.rglob("*.py"):
        if candidate == path:
            continue
        other = ast.parse(candidate.read_text(encoding="utf-8"), filename=str(candidate))
        if any(
            isinstance(node, ast.Name)
            and node.id == "CoordinatedRuntimeReconciliationService"
            for node in ast.walk(other)
        ):
            callers.append(candidate)
    assert callers == []
