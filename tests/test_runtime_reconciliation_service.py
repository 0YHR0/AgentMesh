from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from agentmesh.application.runtime_reconciliation import RuntimeOutcomeReconciliationService
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalType, Role
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
)
from agentmesh.domain.tasks import AttemptStatus, RunRole, RunStatus, TaskExecutionMode, TaskStatus
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import (
    ErrorCategory,
    RetryDisposition,
    RuntimeErrorDTO,
    RuntimeObservation,
    RuntimePhase,
    canonical_digest,
)
from tests.fakes import InMemoryOutboxRepository, InMemoryUnitOfWork
from tests.test_task_service import (
    _managed_direct_finalizer_case,
    _RuntimeAwareFactory,
    _RuntimeRepositoryProbe,
)

TENANT_ID = "runtime-reconciliation-unit"


class _ReconciliationRuntimeRepository(_RuntimeRepositoryProbe):
    """Transaction-local runtime projection used by executable unit tests."""

    def __init__(self, *args, observations=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = list(observations)

    def prior_observations(self, execution_id, *, tenant_id, observation_id, digest):
        return [
            item
            for item in self.observations
            if item.runtime_execution_id == execution_id
            and item.tenant_id == tenant_id
            and (item.observation_id == observation_id or item.observation_digest == digest)
        ]

    def add_observation(self, value):
        self.observations.append(value)

    def update_observation_outcome(self, value, *, outcome):
        index = next(index for index, item in enumerate(self.observations) if item.id == value.id)
        self.observations[index] = replace(value, processing_outcome=outcome)

    def save_execution(self, value, *, tenant_id):
        if tenant_id != self.registry.execution.tenant_id:
            raise AssertionError("cross-tenant runtime write")
        self.registry.execution = deepcopy(value)

    def snapshot(self):
        return {
            "base": super().snapshot(),
            "observations": list(self.observations),
        }

    def restore(self, snapshot):
        super().restore(snapshot["base"])
        self.observations = list(snapshot["observations"])


class _ResearchProbe:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def materialize_if_ready(self, task_id, *, actor):
        self.calls += 1
        if self.fail:
            raise RuntimeError("research materialization unavailable")


class _StateSensitiveMemoryProbe:
    def __init__(self):
        self.calls = 0
        self.tasks = []

    def capture_completed_task_in_unit_of_work(self, uow, task):
        if task.status is not TaskStatus.COMPLETED or task.output != {"answer": 42}:
            raise AssertionError("Memory must receive the persisted completed Task")
        self.calls += 1
        self.tasks.append(task)

    def snapshot(self):
        return self.calls, list(self.tasks)

    def restore(self, snapshot):
        self.calls, self.tasks = snapshot


def _parked_reconciliation_case(*, budget=None, quota=False, resources=()):
    case = _managed_direct_finalizer_case(
        phase=RuntimePhase.OUTCOME_UNKNOWN, budget=budget, quota=quota
    )
    base_factory, tasks, worker, envelope, _result, registry, memory, task_id, attempt = case
    assert (
        worker._finalize_managed(
            envelope,
            task_id=task_id,
            run_id=attempt.run_id,
            attempt_id=attempt.id,
            result=_result,
        )
        is False
    )
    execution = registry.execution
    assert execution is not None
    repo = _ReconciliationRuntimeRepository(
        registry,
        attempt.id,
        attempt.fencing_token,
        cancel_intent=None,
    )
    return (
        base_factory,
        tasks,
        _RuntimeAwareFactory(base_factory, repo, resources=resources),
        registry,
        repo,
        task_id,
        attempt,
        execution,
        memory,
        None,
    )


def _reconciliation_service(factory, *, memory=None, research=None):
    return RuntimeOutcomeReconciliationService(
        uow_factory=factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
        ),
        runtime_memory_service=memory,
        research_materialization_service=research,
    )


def _principal(*, tenant_id: str = TENANT_ID, authenticated: bool = True) -> PrincipalContext:
    return PrincipalContext(
        principal_id="operator-unit",
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        roles=frozenset({Role.OPERATOR}),
        authenticated=authenticated,
        authentication_method="test",
    )


def _service() -> RuntimeOutcomeReconciliationService:
    def reject_uow():
        raise AssertionError("invalid requests must be rejected before opening a UoW")

    return RuntimeOutcomeReconciliationService(
        uow_factory=reject_uow,
        tenant_id=TENANT_ID,
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,outcome_reconciliation=true,identity_rbac=true",
        ),
    )


def _observation(
    execution_id,
    *,
    phase: RuntimePhase = RuntimePhase.SUCCEEDED,
    output=None,
    usage=None,
) -> RuntimeObservation:
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution_id),
        assignment_id=str(uuid4()),
        assignment_digest="a" * 64,
        phase=phase,
        observed_at=datetime.now(timezone.utc),
        snapshot_digest="b" * 64,
        output={"answer": 42} if output is None and phase is RuntimePhase.SUCCEEDED else output,
        usage={} if usage is None else usage,
    )


def _reconcile(service, execution_id, observation, **overrides):
    values = {
        "principal": _principal(),
        "observation": observation,
        "evidence_digest": canonical_digest(observation.to_dict()),
        "evidence_reference": "case://unit/evidence",
        "reason": "Provider support supplied canonical evidence",
        "idempotency_key": "unit-reconcile-1",
    }
    values.update(overrides)
    return service.reconcile_outcome(execution_id, **values)


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        (_principal(authenticated=False), AuthorizationDenied),
        (_principal(tenant_id="another-tenant"), AuthorizationDenied),
    ],
)
def test_runtime_reconciliation_rejects_invalid_principal_before_uow(principal, expected) -> None:
    execution_id = uuid4()
    observation = _observation(execution_id)
    with pytest.raises(expected):
        _reconcile(_service(), execution_id, observation, principal=principal)


def test_runtime_reconciliation_rejects_nonterminal_and_wrong_execution_before_uow() -> None:
    service = _service()
    execution_id = uuid4()
    running = _observation(execution_id, phase=RuntimePhase.RUNNING)
    with pytest.raises(InvalidTaskInput, match="known terminal"):
        _reconcile(service, execution_id, running)

    other_execution = _observation(uuid4())
    with pytest.raises(InvalidTaskInput, match="execution identity"):
        _reconcile(service, execution_id, other_execution)


def test_runtime_reconciliation_rejects_digest_and_success_shape_before_uow() -> None:
    service = _service()
    execution_id = uuid4()
    observation = _observation(execution_id)
    with pytest.raises(InvalidTaskInput, match="canonical observation digest"):
        _reconcile(service, execution_id, observation, evidence_digest="f" * 64)

    non_mapping = _observation(execution_id, output=["not", "a", "mapping"])
    with pytest.raises(InvalidTaskInput, match="mapping output"):
        _reconcile(service, execution_id, non_mapping)

    billed = _observation(execution_id, usage={"input_tokens": 1})
    with pytest.raises(InvalidTaskInput, match="empty usage"):
        _reconcile(service, execution_id, billed)


@pytest.mark.parametrize(
    "phase",
    [
        RuntimePhase.SUCCEEDED,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELED,
        RuntimePhase.TIMED_OUT,
    ],
)
def test_runtime_reconciliation_rejects_usage_for_every_terminal_phase(phase) -> None:
    service = _service()
    execution_id = uuid4()
    observation = _observation(execution_id, phase=phase, usage={"input_tokens": 1})
    with pytest.raises(InvalidTaskInput, match="empty usage"):
        _reconcile(service, execution_id, observation)


@pytest.mark.parametrize(
    "field",
    ["governed_action_requests", "wait_refs"],
)
def test_runtime_reconciliation_rejects_unresolved_terminal_requests(field) -> None:
    service = _service()
    execution_id = uuid4()
    values = {
        "governed_action_requests": ({"action": "pending"},),
        "wait_refs": ("wait://pending",),
    }
    observation = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution_id),
        assignment_id=str(uuid4()),
        assignment_digest="a" * 64,
        phase=RuntimePhase.FAILED,
        observed_at=datetime.now(timezone.utc),
        snapshot_digest="b" * 64,
        **{field: values[field]},
    )
    with pytest.raises(InvalidTaskInput, match="action or wait requests"):
        _reconcile(service, execution_id, observation)


def test_runtime_reconciliation_rejects_success_with_error() -> None:
    service = _service()
    execution_id = uuid4()
    observation = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution_id),
        assignment_id=str(uuid4()),
        assignment_digest="a" * 64,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=datetime.now(timezone.utc),
        snapshot_digest="b" * 64,
        output={"answer": 42},
        error=RuntimeErrorDTO(
            code="provider.error",
            category=ErrorCategory.PERMANENT,
            message="must not accompany success",
            retry_disposition=RetryDisposition.NEVER,
        ),
    )
    with pytest.raises(InvalidTaskInput, match="cannot carry an error"):
        _reconcile(service, execution_id, observation)


def _parked_observation(execution, phase):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
        provider_event_id=f"unit-{phase.value.lower()}-{uuid4().hex}",
        output={"answer": 42} if phase is RuntimePhase.SUCCEEDED else None,
    )


def _business_projection(aggregate):
    task = aggregate.task
    run = aggregate.runs[0]
    attempt = aggregate.attempts[0]
    return {
        "task": (
            task.status,
            task.version,
            task.updated_at,
            task.output,
            task.error,
            task.candidate_output,
            task.current_run_id,
            task.reserved_tokens,
            task.reserved_cost_micros,
            task.settled_tokens,
            task.settled_cost_micros,
        ),
        "run": (
            run.status,
            run.queued_at,
            run.started_at,
            run.completed_at,
            run.error,
            run.output,
        ),
        "attempt": (
            attempt.status,
            attempt.heartbeat_at,
            attempt.started_at,
            attempt.completed_at,
            attempt.error,
            attempt.reserved_tokens,
            attempt.reserved_cost_micros,
            attempt.settled_tokens,
            attempt.settled_cost_micros,
            attempt.budget_settlement_source,
        ),
    }


@pytest.mark.parametrize(
    ("phase", "expected_task", "expected_run", "expected_attempt"),
    [
        (
            RuntimePhase.SUCCEEDED,
            TaskStatus.COMPLETED,
            RunStatus.SUCCEEDED,
            AttemptStatus.SUCCEEDED,
        ),
        (
            RuntimePhase.FAILED,
            TaskStatus.FAILED,
            RunStatus.FAILED,
            AttemptStatus.FAILED,
        ),
        (
            RuntimePhase.CANCELED,
            TaskStatus.FAILED,
            RunStatus.FAILED,
            AttemptStatus.FAILED,
        ),
        (
            RuntimePhase.TIMED_OUT,
            TaskStatus.FAILED,
            RunStatus.FAILED,
            AttemptStatus.FAILED,
        ),
    ],
)
def test_reconciliation_real_parked_direct_chain_maps_terminal_once(
    phase, expected_task, expected_run, expected_attempt
):
    (
        base_factory,
        tasks,
        factory,
        registry,
        repo,
        task_id,
        attempt,
        execution,
        _memory,
        _phase,
    ) = _parked_reconciliation_case()
    memory = _StateSensitiveMemoryProbe()
    observation = _parked_observation(execution, phase)
    service = _reconciliation_service(factory, memory=memory)
    result = service.reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/parked",
        reason="Operator confirmed provider outcome",
        idempotency_key=f"parked-{phase.value.lower()}",
    )
    replay = service.reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/parked",
        reason="Operator confirmed provider outcome",
        idempotency_key=f"parked-{phase.value.lower()}",
    )
    aggregate = tasks.get_task(task_id)
    assert result.resolution.id == replay.resolution.id
    assert aggregate.task.status is expected_task
    assert result.resolution.resulting_status is expected_task
    assert aggregate.runs[0].status is expected_run
    assert aggregate.attempts[0].status is expected_attempt
    assert len(repo.observations) == 1
    assert aggregate.task.updated_at == result.resolution.created_at
    assert aggregate.runs[0].completed_at == result.resolution.created_at
    assert aggregate.attempts[0].completed_at == result.resolution.created_at
    assert registry.execution.updated_at == result.resolution.created_at
    assert result.resolution.created_at < observation.observed_at
    expected_reason = {
        RuntimePhase.SUCCEEDED: "runtime.confirmed_success",
        RuntimePhase.FAILED: "runtime.reconciled_failed",
        RuntimePhase.CANCELED: "runtime.unrequested_cancellation",
        RuntimePhase.TIMED_OUT: "runtime.reconciled_timed_out",
    }[phase]
    assert result.resolution.details["business_mapping_reason"] == expected_reason
    assert memory.calls == int(phase is RuntimePhase.SUCCEEDED)
    if phase is RuntimePhase.SUCCEEDED:
        assert memory.tasks[0].status is TaskStatus.COMPLETED
        assert memory.tasks[0].output == {"answer": 42}
    assert len(base_factory.store.task_resolutions) == 1
    events = [
        item
        for item in base_factory.store.outbox
        if item.schema_name == "agentmesh.runtime.outcome-reconciled"
    ]
    assert len(events) == 1
    assert events[0].occurred_at == result.resolution.created_at
    assert events[0].causation_id == uuid5(
        NAMESPACE_URL, f"runtime-reconcile:{execution.id}:parked-{phase.value.lower()}"
    )


def test_reconciliation_lost_parked_direct_chain_uses_control_plane_convergence():
    case = _parked_reconciliation_case()
    base_factory, tasks, factory, registry, repo, task_id, attempt, execution, _memory, _ = case
    assert registry.execution is not None
    registry.execution = replace(
        registry.execution,
        phase=RuntimeExecutionPhase.LOST,
    )
    observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    result = _reconciliation_service(factory).reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/lost-parked",
        reason="Operator confirmed outcome after lost execution",
        idempotency_key="lost-parked-once",
    )

    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.COMPLETED
    assert aggregate.runs[0].status is RunStatus.SUCCEEDED
    assert aggregate.attempts[0].status is AttemptStatus.SUCCEEDED
    assert result.resolution.details["previous_phase"] == RuntimeExecutionPhase.LOST.value
    assert len(repo.observations) == 1
    assert len(base_factory.store.task_resolutions) == 1


def test_reconciliation_parked_budget_never_resettles_or_releases_again():
    budget = TaskBudget.create(max_tokens=100, token_reservation_per_attempt=10)
    case = _parked_reconciliation_case(budget=budget, quota=True)
    (
        base_factory,
        tasks,
        factory,
        registry,
        repo,
        task_id,
        attempt,
        execution,
        _memory,
        _phase,
    ) = case
    parked = tasks.get_task(task_id)
    source = parked.attempts[0].budget_settlement_source
    settled = parked.task.settled_tokens
    reserved = parked.task.reserved_tokens
    reservations_before = deepcopy(base_factory.store.quota_reservations)
    assert len(reservations_before) == 2
    service = _reconciliation_service(factory)
    observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    service.reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/conservative",
        reason="Conservative accounting was already settled",
        idempotency_key="conservative-once",
    )
    service.reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/conservative",
        reason="Conservative accounting was already settled",
        idempotency_key="conservative-once",
    )
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.settled_tokens == settled
    assert aggregate.task.reserved_tokens == reserved
    assert aggregate.attempts[0].budget_settlement_source is source
    assert base_factory.store.quota_reservations == reservations_before


def test_reconciliation_requested_cancel_uses_persisted_intent():
    case = _parked_reconciliation_case()
    base_factory, tasks, factory, registry, repo, task_id, attempt, execution, _memory, _ = case
    repo.cancel_intent = object()
    observation = _parked_observation(execution, RuntimePhase.CANCELED)
    service = _reconciliation_service(factory)
    result = service.reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/requested-cancel",
        reason="Operator confirmed requested cancellation",
        idempotency_key="requested-cancel-once",
    )
    aggregate = tasks.get_task(task_id)
    assert aggregate.task.status is TaskStatus.CANCELED
    assert aggregate.runs[0].status is RunStatus.CANCELED
    assert aggregate.attempts[0].status is AttemptStatus.CANCELED
    assert result.resolution.details["business_mapping_reason"] == "runtime.reconciled_canceled"


def test_reconciliation_rejects_wrong_current_run_without_writes():
    case = _parked_reconciliation_case()
    base_factory, tasks, factory, registry, repo, task_id, attempt, execution, _memory, _ = case
    stored_task = base_factory.store.tasks[task_id]
    base_factory.store.tasks[task_id] = replace(stored_task, current_run_id=uuid4())
    observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    with pytest.raises(InvalidTaskTransition):
        _reconciliation_service(factory).reconcile_outcome(
            execution.id,
            principal=_principal(tenant_id="test-tenant"),
            observation=observation,
            evidence_digest=canonical_digest(observation.to_dict()),
            evidence_reference="case://unit/current-run",
            reason="Wrong current run",
            idempotency_key="wrong-current-run",
        )
    assert repo.observations == []
    assert base_factory.store.task_resolutions == {}


def test_reconciliation_outbox_failure_rolls_back_runtime_and_business_then_replays(monkeypatch):
    case = _parked_reconciliation_case()
    base_factory, tasks, _factory, registry, repo, task_id, attempt, execution, memory, _ = case
    factory = _RuntimeAwareFactory(base_factory, repo, resources=(memory,))
    observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    before_task = tasks.get_task(task_id)
    before_execution = registry.execution
    before_outbox = len(base_factory.store.outbox)

    def fail_after_business_write(self, envelope):
        raise RuntimeError("reconciliation outbox unavailable")

    monkeypatch.setattr(InMemoryOutboxRepository, "add", fail_after_business_write)
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        _reconciliation_service(factory, memory=memory).reconcile_outcome(
            execution.id,
            principal=_principal(tenant_id="test-tenant"),
            observation=observation,
            evidence_digest=canonical_digest(observation.to_dict()),
            evidence_reference="case://unit/rollback",
            reason="Rollback probe",
            idempotency_key="rollback-once",
        )
    after_failure = tasks.get_task(task_id)
    assert after_failure.task.status is before_task.task.status
    assert after_failure.task.version == before_task.task.version
    assert registry.execution == before_execution
    assert repo.observations == []
    assert base_factory.store.task_resolutions == {}
    assert len(base_factory.store.outbox) == before_outbox
    assert base_factory.store.idempotency == {}
    monkeypatch.undo()
    service = _reconciliation_service(factory)
    service.reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/rollback",
        reason="Rollback probe",
        idempotency_key="rollback-once",
    )
    assert tasks.get_task(task_id).task.status is TaskStatus.COMPLETED
    assert len(base_factory.store.task_resolutions) == 1


def test_reconciliation_commit_failure_rolls_back_after_memory_and_replays(monkeypatch):
    budget = TaskBudget.create(max_tokens=100, token_reservation_per_attempt=10)
    case = _parked_reconciliation_case(budget=budget, quota=True)
    base_factory, tasks, _factory, registry, repo, task_id, attempt, execution, _memory, _ = case
    memory = _StateSensitiveMemoryProbe()
    factory = _RuntimeAwareFactory(base_factory, repo, resources=(memory,))
    observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    initial_outbox = len(base_factory.store.outbox)
    before_projection = _business_projection(tasks.get_task(task_id))
    quota_before = deepcopy(base_factory.store.quota_reservations)
    assert len(quota_before) == 2

    def fail_before_publish(self):
        raise RuntimeError("commit unavailable")

    monkeypatch.setattr(InMemoryUnitOfWork, "commit", fail_before_publish)
    with pytest.raises(RuntimeError, match="commit unavailable"):
        _reconciliation_service(factory, memory=memory).reconcile_outcome(
            execution.id,
            principal=_principal(tenant_id="test-tenant"),
            observation=observation,
            evidence_digest=canonical_digest(observation.to_dict()),
            evidence_reference="case://unit/commit-rollback",
            reason="Commit failure probe",
            idempotency_key="commit-rollback-once",
        )
    assert _business_projection(tasks.get_task(task_id)) == before_projection
    assert tasks.get_task(task_id).task.status is TaskStatus.RECONCILIATION_REQUIRED
    assert base_factory.store.quota_reservations == quota_before
    assert memory.calls == 0
    assert registry.execution == execution
    assert repo.observations == []
    assert base_factory.store.task_resolutions == {}
    assert base_factory.store.idempotency == {}
    assert len(base_factory.store.outbox) == initial_outbox
    monkeypatch.undo()
    _reconciliation_service(factory, memory=memory).reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/commit-rollback",
        reason="Commit failure probe",
        idempotency_key="commit-rollback-once",
    )
    assert memory.calls == 1
    assert len(base_factory.store.task_resolutions) == 1


def test_reconciliation_research_is_post_commit_best_effort_and_not_replayed():
    case = _parked_reconciliation_case()
    base_factory, tasks, factory, registry, repo, task_id, attempt, execution, _memory, _ = case
    research = _ResearchProbe()
    service = _reconciliation_service(factory, research=research)
    observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    kwargs = dict(
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/research",
        reason="Research completion probe",
        idempotency_key="research-once",
    )
    service.reconcile_outcome(execution.id, **kwargs)
    service.reconcile_outcome(execution.id, **kwargs)
    assert research.calls == 1
    assert tasks.get_task(task_id).task.status is TaskStatus.COMPLETED

    failure_case = _parked_reconciliation_case()
    fbase, ftasks, ffactory, _fregistry, _frepo, fid, fattempt, fexecution, _fmemory, _ = (
        failure_case
    )
    failing = _ResearchProbe(fail=True)
    fservice = _reconciliation_service(ffactory, research=failing)
    fobservation = _parked_observation(fexecution, RuntimePhase.SUCCEEDED)
    fservice.reconcile_outcome(
        fexecution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=fobservation,
        evidence_digest=canonical_digest(fobservation.to_dict()),
        evidence_reference="case://unit/research-failure",
        reason="Best effort research failure",
        idempotency_key="research-failure-once",
    )
    assert failing.calls == 1
    assert ftasks.get_task(fid).task.status is TaskStatus.COMPLETED


@pytest.mark.parametrize(
    "phase",
    [
        RuntimePhase.SUCCEEDED,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELED,
        RuntimePhase.TIMED_OUT,
    ],
)
def test_canceled_runtime_only_known_conclusions_are_evidence_only(phase):
    budget = TaskBudget.create(max_tokens=100, token_reservation_per_attempt=10)
    case = _parked_reconciliation_case(budget=budget, quota=True)
    base_factory, tasks, factory, registry, repo, task_id, attempt, execution, memory, _ = case
    task = base_factory.store.tasks[task_id]
    run = base_factory.store.runs[attempt.run_id]
    base_factory.store.tasks[task_id] = replace(task, status=TaskStatus.CANCELED)
    base_factory.store.runs[attempt.run_id] = replace(run, status=RunStatus.CANCELED)
    base_factory.store.attempts[attempt.id] = replace(
        base_factory.store.attempts[attempt.id],
        status=AttemptStatus.CANCELED,
        settled_tokens=0,
        settled_cost_micros=0,
        budget_settlement_source=BudgetSettlementSource.RELEASED,
    )
    base_factory.store.tasks[task_id] = replace(
        base_factory.store.tasks[task_id], settled_tokens=0, settled_cost_micros=0
    )
    repo.cancel_intent = object()
    memory = _StateSensitiveMemoryProbe()
    research = _ResearchProbe()
    before = tasks.get_task(task_id)
    before_projection = _business_projection(before)
    quota_before = deepcopy(base_factory.store.quota_reservations)
    outbox_before = len(base_factory.store.outbox)
    assert len(quota_before) == 2
    observation = _parked_observation(execution, phase)
    service = _reconciliation_service(factory, memory=memory, research=research)
    result = service.reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/canceled-runtime-only",
        reason="Canceled task runtime conclusion",
        idempotency_key=f"canceled-runtime-only-{phase.value.lower()}",
    )
    service.reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=canonical_digest(observation.to_dict()),
        evidence_reference="case://unit/canceled-runtime-only",
        reason="Canceled task runtime conclusion",
        idempotency_key=f"canceled-runtime-only-{phase.value.lower()}",
    )
    after = tasks.get_task(task_id)
    assert _business_projection(after) == before_projection
    assert base_factory.store.quota_reservations == quota_before
    assert after.task.status is before.task.status is TaskStatus.CANCELED
    assert after.runs[0].status is before.runs[0].status is RunStatus.CANCELED
    assert after.attempts[0].status is before.attempts[0].status is AttemptStatus.CANCELED
    assert memory.calls == 0
    assert research.calls == 0
    assert result.resolution.resulting_status is TaskStatus.CANCELED
    if phase is RuntimePhase.SUCCEEDED:
        assert repo.observations[0].evidence["quarantined_output"] == {"answer": 42}
    else:
        assert "quarantined_output" not in repo.observations[0].evidence
    assert len(repo.observations) == 1
    assert len(base_factory.store.task_resolutions) == 1
    assert (
        len(
            [
                item
                for item in base_factory.store.outbox
                if item.schema_name == "agentmesh.runtime.outcome-reconciled"
            ]
        )
        == 1
    )
    new_events = base_factory.store.outbox[outbox_before:]
    assert len(new_events) == 1
    assert new_events[0].schema_name == "agentmesh.runtime.outcome-reconciled"
    relevant_events = [
        item for item in base_factory.store.outbox if item.payload.get("task_id") == str(task_id)
    ]
    assert {item.schema_name for item in relevant_events} <= {
        "agentmesh.runtime.reconciliation.required",
        "agentmesh.runtime.outcome-reconciled",
        "agentmesh.run.requested",
    }


def test_reconciliation_runtime_only_requires_persisted_cancel_intent():
    case = _parked_reconciliation_case()
    base_factory, tasks, factory, registry, repo, task_id, attempt, execution, _memory, _ = case
    task = base_factory.store.tasks[task_id]
    run = base_factory.store.runs[attempt.run_id]
    base_factory.store.tasks[task_id] = replace(task, status=TaskStatus.CANCELED)
    base_factory.store.runs[attempt.run_id] = replace(run, status=RunStatus.CANCELED)
    base_factory.store.attempts[attempt.id] = replace(
        base_factory.store.attempts[attempt.id], status=AttemptStatus.CANCELED
    )
    observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    with pytest.raises(InvalidTaskTransition, match="strictly consistent"):
        _reconciliation_service(factory).reconcile_outcome(
            execution.id,
            principal=_principal(tenant_id="test-tenant"),
            observation=observation,
            evidence_digest=canonical_digest(observation.to_dict()),
            evidence_reference="case://unit/no-intent",
            reason="No persisted intent",
            idempotency_key="canceled-no-intent",
        )
    assert repo.observations == []
    assert base_factory.store.task_resolutions == {}
    assert not [
        item
        for item in base_factory.store.outbox
        if item.schema_name == "agentmesh.runtime.outcome-reconciled"
    ]


@pytest.mark.parametrize(
    "mode", ["legacy", "wrong-owner", "wrong-fence", "reviewed", "reviewer", "subtask"]
)
def test_reconciliation_rejects_invalid_chain_before_writes(mode):
    for _ in (0,):
        case = _parked_reconciliation_case()
        base_factory, tasks, factory, registry, repo, task_id, attempt, execution, _memory, _ = case
        before_projection = _business_projection(tasks.get_task(task_id))
        before_execution = registry.execution
        if mode == "legacy":
            stored_run = base_factory.store.runs[attempt.run_id]
            base_factory.store.runs[attempt.run_id] = replace(
                stored_run, runtime_authority="legacy"
            )
        elif mode == "wrong-owner":
            repo.owner_attempt_id = uuid4()
        elif mode == "wrong-fence":
            repo.fencing_token += 1
        elif mode == "reviewed":
            stored_task = base_factory.store.tasks[task_id]
            base_factory.store.tasks[task_id] = replace(
                stored_task, execution_mode=TaskExecutionMode.REVIEWED
            )
        elif mode == "reviewer":
            stored_run = base_factory.store.runs[attempt.run_id]
            base_factory.store.runs[attempt.run_id] = replace(stored_run, role=RunRole.REVIEWER)
        else:
            stored_run = base_factory.store.runs[attempt.run_id]
            base_factory.store.runs[attempt.run_id] = replace(stored_run, subtask_id=uuid4())
        observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
        with pytest.raises(InvalidTaskTransition):
            _reconciliation_service(factory).reconcile_outcome(
                execution.id,
                principal=_principal(tenant_id="test-tenant"),
                observation=observation,
                evidence_digest=canonical_digest(observation.to_dict()),
                evidence_reference="case://unit/invalid-chain",
                reason="Invalid parked chain",
                idempotency_key=f"invalid-chain-{mode}",
            )
        assert repo.observations == []
        assert base_factory.store.task_resolutions == {}
        assert _business_projection(tasks.get_task(task_id)) == before_projection
        assert registry.execution == before_execution
        assert not [
            item
            for item in base_factory.store.outbox
            if item.schema_name == "agentmesh.runtime.outcome-reconciled"
        ]


def test_reconciliation_rejects_disallowed_accounting_state_before_writes():
    case = _parked_reconciliation_case()
    base_factory, tasks, factory, registry, repo, task_id, attempt, execution, _memory, _ = case
    stored_attempt = base_factory.store.attempts[attempt.id]
    base_factory.store.attempts[attempt.id] = replace(
        stored_attempt, budget_settlement_source=BudgetSettlementSource.RELEASED
    )
    observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    with pytest.raises(InvalidTaskTransition):
        _reconciliation_service(factory).reconcile_outcome(
            execution.id,
            principal=_principal(tenant_id="test-tenant"),
            observation=observation,
            evidence_digest=canonical_digest(observation.to_dict()),
            evidence_reference="case://unit/accounting-invalid",
            reason="Accounting state is not applicable",
            idempotency_key="accounting-invalid",
        )
    assert repo.observations == []
    assert base_factory.store.task_resolutions == {}
    assert not [
        item
        for item in base_factory.store.outbox
        if item.schema_name == "agentmesh.runtime.outcome-reconciled"
    ]


def test_late_success_after_exact_conflict_gets_one_deterministic_quarantine_row():
    case = _parked_reconciliation_case()
    base_factory, tasks, factory, registry, repo, task_id, attempt, execution, _memory, _ = case
    task = base_factory.store.tasks[task_id]
    run = base_factory.store.runs[attempt.run_id]
    base_factory.store.tasks[task_id] = replace(task, status=TaskStatus.CANCELED)
    base_factory.store.runs[attempt.run_id] = replace(run, status=RunStatus.CANCELED)
    base_factory.store.attempts[attempt.id] = replace(
        base_factory.store.attempts[attempt.id], status=AttemptStatus.CANCELED
    )
    repo.cancel_intent = object()
    observation = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    digest = canonical_digest(observation.to_dict())
    repo.observations.append(
        RuntimeObservationEvidence(
            id=uuid4(),
            tenant_id="test-tenant",
            runtime_execution_id=execution.id,
            observation_id=observation.observation_id,
            observation_digest=digest,
            assignment_id=execution.assignment_id,
            assignment_digest=execution.assignment_digest,
            provider_sequence=observation.provider_sequence,
            phase=RuntimeExecutionPhase.SUCCEEDED,
            observed_at=observation.observed_at,
            received_at=observation.observed_at,
            safe_summary="prior conflict",
            processing_outcome=RuntimeObservationOutcome.CONFLICT,
            provider_event_present=observation.provider_event_id is not None,
            evidence={
                "provider_event_id": observation.provider_event_id,
                "snapshot_digest": observation.snapshot_digest,
            },
        )
    )
    service = _reconciliation_service(factory)
    kwargs = dict(
        principal=_principal(tenant_id="test-tenant"),
        observation=observation,
        evidence_digest=digest,
        evidence_reference="case://unit/late-success",
        reason="Late provider success",
        idempotency_key="late-success-once",
    )
    service.reconcile_outcome(execution.id, **kwargs)
    service.reconcile_outcome(execution.id, **kwargs)
    quarantine = [
        item for item in repo.observations if item.observation_id.endswith(":quarantined-output")
    ]
    assert len(quarantine) == 1
    assert quarantine[0].id == uuid5(NAMESPACE_URL, f"{repo.observations[0].id}:quarantined-output")


def test_reconciliation_competing_conclusion_is_idempotency_conflict():
    case = _parked_reconciliation_case()
    base_factory, tasks, factory, registry, repo, task_id, attempt, execution, _memory, _ = case
    service = _reconciliation_service(factory)
    first = _parked_observation(execution, RuntimePhase.SUCCEEDED)
    service.reconcile_outcome(
        execution.id,
        principal=_principal(tenant_id="test-tenant"),
        observation=first,
        evidence_digest=canonical_digest(first.to_dict()),
        evidence_reference="case://unit/competing",
        reason="First conclusion",
        idempotency_key="competing-conclusion",
    )
    competing = _parked_observation(execution, RuntimePhase.FAILED)
    with pytest.raises(IdempotencyConflict):
        service.reconcile_outcome(
            execution.id,
            principal=_principal(tenant_id="test-tenant"),
            observation=competing,
            evidence_digest=canonical_digest(competing.to_dict()),
            evidence_reference="case://unit/competing",
            reason="Competing conclusion",
            idempotency_key="competing-conclusion",
        )
    assert len(base_factory.store.task_resolutions) == 1
    assert len(repo.observations) == 1
