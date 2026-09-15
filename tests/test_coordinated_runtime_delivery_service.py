"""Public-process call matrix for coordinated runtime delivery."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedKnownTerminalKind,
    CoordinatedKnownTerminalResult,
)
from agentmesh.application.coordinated_runtime_delivery import (
    CoordinatedDeliveryLeaseV1,
    CoordinatedDeliveryResult,
    CoordinatedDeliveryResultKind,
    DeliveryInProgress,
    RecoveryCrossedProof,
    assignment_projection_digest,
    ownership_digest,
    stable_dispatch_identity,
)
from agentmesh.application.coordinated_runtime_delivery_service import (
    CoordinatedRuntimeDeliveryService,
)
from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedRuntimeBindReceiptKind,
    CoordinatedRuntimeBindReceiptResult,
    CoordinatedRuntimeDispatchKind,
    CoordinatedRuntimeDispatchResult,
    CoordinatedRuntimePrepareKind,
    CoordinatedRuntimePrepareResult,
)
from agentmesh.application.coordinated_runtime_predispatch_failure import (
    CoordinatedPredispatchFailureKind,
    CoordinatedPredispatchFailureResult,
)
from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedUnknownOutcomeKind,
    CoordinatedUnknownOutcomeResult,
)
from agentmesh.application.ports import ManagedRuntimeConflictObservation, WorkflowWorkItem
from agentmesh.application.runtime_snapshots import handle_snapshot_for
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import RunRole
from agentmesh.runtime_sdk import (
    ErrorCategory,
    RetryDisposition,
    RuntimeAssignment,
    RuntimeExecutionHandle,
    RuntimeObservation,
    RuntimePhase,
    ValidationReport,
)
from agentmesh.runtime_sdk import (
    RuntimeError as RuntimeErrorDTO,
)

UTC = timezone.utc
NOW = datetime(2030, 1, 1, tzinfo=UTC)


class Log:
    def __init__(self):
        self.events = []

    def add(self, name):
        self.events.append(name)


def objects():
    task, run, subtask, attempt, execution, version, agent = (uuid4() for _ in range(7))
    work = WorkflowWorkItem("objective", {"x": 1})
    projection = assignment_projection_digest(
        tenant_id="tenant",
        task_id=task,
        run_id=run,
        subtask_id=subtask,
        role=RunRole.EXECUTOR,
        runtime_version_id=version,
        runtime_execution_intent_id=execution,
        agent_version_id=agent,
        agent_version_digest="a" * 64,
        task_plan_version=1,
        task_plan_digest="sha256:" + "b" * 64,
        run_revision=0,
        work_item=work,
    )
    token, deadline = uuid4(), NOW + timedelta(minutes=5)
    lease = CoordinatedDeliveryLeaseV1(
        schema_version=1,
        tenant_id="tenant",
        task_id=task,
        run_id=run,
        subtask_id=subtask,
        attempt_id=attempt,
        role=RunRole.EXECUTOR,
        fencing_token=3,
        lease_token=token,
        lease_deadline=deadline,
        runtime_version_id=version,
        runtime_execution_intent_id=execution,
        task_plan_version=1,
        task_plan_digest="sha256:" + "b" * 64,
        run_revision=0,
        agent_version_id=agent,
        agent_version_digest="a" * 64,
        work_item=work,
        assignment_projection_digest=projection,
        ownership_digest=ownership_digest(
            assignment_projection_digest=projection,
            tenant_id="tenant",
            task_id=task,
            run_id=run,
            subtask_id=subtask,
            attempt_id=attempt,
            fencing_token=3,
            lease_token=token,
            lease_deadline=deadline,
        ),
    )
    assignment = RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id="tenant",
        task_id=str(task),
        run_id=str(run),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(agent),
        agent_version_digest="a" * 64,
        runtime_version_id=str(version),
        runtime_descriptor_digest="c" * 64,
        execution_mode="inline",
        run_role="EXECUTOR",
        revision=0,
        objective="objective",
        structured_input={"x": 1},
        correlation_ids={"runtime_execution_id": str(execution)},
        extensions={"coordinated_delivery": {"assignment_projection_digest": projection}},
    )
    handle = RuntimeExecutionHandle(
        runtime_execution_id=str(execution),
        runtime_version_id=str(version),
        provider_execution_ref="provider",
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest,
        created_at=NOW,
    )
    return lease, assignment, handle


def envelope(lease):
    return MessageEnvelope.run_requested(
        tenant_id=lease.tenant_id, task_id=lease.task_id, run_id=lease.run_id, at=NOW
    )


def observation(lease, assignment, phase=RuntimePhase.SUCCEEDED, at=NOW):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(lease.runtime_execution_intent_id),
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest,
        phase=phase,
        observed_at=at,
        provider_event_id="event",
        output={} if phase is RuntimePhase.SUCCEEDED else None,
    )


def typed_result(cls, kind, execution_id=None, **identity):
    """Build collaborator result shells without duplicating aggregate fields."""
    result = object.__new__(cls)
    object.__setattr__(result, "kind", kind)
    if execution_id is not None:
        object.__setattr__(result, "execution_id", execution_id)
    for name, value in identity.items():
        object.__setattr__(result, name, value)
    return result


def service(
    lease,
    assignment,
    log,
    *,
    acquisition=None,
    prepare=CoordinatedRuntimePrepareKind.PREPARED,
    crossed=CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED,
    receipt=None,
    inspected=None,
    valid=True,
    failure=None,
    convergence=None,
    unknown=None,
    snapshots=None,
    clock=lambda: NOW,
):
    class Acq:
        def classify_and_acquire(self, env, *, now):
            log.add("acquire")
            return acquisition.pop(0)

    class Managed:
        def assignment_for_delivery(self, *_):
            log.add("assignment")
            return assignment

        def bind_delivery_context(self, *_):
            log.add("bind_context")

    class Adapter:
        def validate(self, _):
            log.add("validate")
            return ValidationReport(valid=valid)

        def dispatch(self, *_args, **_kw):
            log.add("dispatch")
            return receipt

        def inspect(self, _):
            log.add("inspect")
            return inspected

    class Dispatch:
        def prepare_runtime_assignment(self, **_):
            log.add("prepare")
            return typed_result(
                CoordinatedRuntimePrepareResult, prepare, lease.runtime_execution_intent_id
            )

        def cross_runtime_dispatch_boundary(self, **_):
            log.add("cross")
            return typed_result(
                CoordinatedRuntimeDispatchResult, crossed, lease.runtime_execution_intent_id
            )

        def bind_dispatch_receipt(self, **_):
            log.add("bind_receipt")
            return CoordinatedRuntimeBindReceiptResult(
                kind=CoordinatedRuntimeBindReceiptKind.BOUND,
                execution_id=lease.runtime_execution_intent_id,
            )

    class Failure:
        def fail_delivery(self, *_):
            log.add("fail")
            return failure or typed_result(
                CoordinatedPredispatchFailureResult,
                CoordinatedPredispatchFailureKind.FAILED,
                tenant_id=lease.tenant_id,
                task_id=lease.task_id,
                run_id=lease.run_id,
                attempt_id=lease.attempt_id,
            )

    class Conv:
        def __init__(self):
            self.calls = []

        def apply_delivery_terminal(self, **_):
            log.add("convergence")
            self.calls.append(_)
            return convergence or typed_result(
                CoordinatedKnownTerminalResult,
                CoordinatedKnownTerminalKind.APPLIED,
                tenant_id=lease.tenant_id,
                task_id=lease.task_id,
                run_id=lease.run_id,
                attempt_id=lease.attempt_id,
                runtime_execution_id=lease.runtime_execution_intent_id,
            )

    class Unknown:
        def __init__(self):
            self.calls = []

        def park_delivery_unknown(self, **_):
            log.add("unknown")
            self.calls.append(_)
            return unknown or typed_result(
                CoordinatedUnknownOutcomeResult,
                CoordinatedUnknownOutcomeKind.PARKED,
                tenant_id=lease.tenant_id,
                task_id=lease.task_id,
                run_id=lease.run_id,
                attempt_id=lease.attempt_id,
                runtime_execution_id=lease.runtime_execution_intent_id,
            )

    return CoordinatedRuntimeDeliveryService(
        acquisition_service=Acq(),
        managed_execution_port=Managed(),
        adapter=Adapter(),
        dispatch_service=Dispatch(),
        predispatch_failure_service=Failure(),
        convergence_service=Conv(),
        unknown_service=Unknown(),
        handle_snapshot_reader=lambda _: snapshots,
        consumer_name="consumer",
        utc_clock=clock,
    )


def receipt(lease, assignment, handle, obs=None):
    dispatch_key, _ = stable_dispatch_identity(
        lease.tenant_id, lease.runtime_execution_intent_id, assignment.assignment_digest
    )
    return SimpleNamespace(
        dispatch_key=dispatch_key,
        runtime_execution_id=str(lease.runtime_execution_intent_id),
        assignment_digest=assignment.assignment_digest,
        handle=handle,
        observation=obs,
    )


@pytest.mark.parametrize(
    "kind",
    [
        CoordinatedDeliveryResultKind.NOT_APPLICABLE,
        CoordinatedDeliveryResultKind.REPLAY_PROCESSED,
        CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN,
        CoordinatedDeliveryResultKind.WAITING_APPROVAL,
    ],
)
def test_public_acquisition_not_applicable_replay_blocked_waiting(kind):
    lease, assignment, _ = objects()
    log = Log()
    result = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.without_lease(kind, reason="drain")],
    ).process(envelope(lease))
    assert result.kind.value in {"NOT_APPLICABLE", "REPLAY", "BLOCKED_BY_DRAIN"}
    assert log.events == ["acquire"]


def test_in_progress_and_mismatched_lease_fail_closed():
    lease, assignment, _ = objects()
    log = Log()
    with pytest.raises(DeliveryInProgress):
        service(
            lease,
            assignment,
            log,
            acquisition=[
                CoordinatedDeliveryResult.without_lease(CoordinatedDeliveryResultKind.IN_PROGRESS)
            ],
        ).process(envelope(lease))
    bad = CoordinatedDeliveryResult.acquired(objects()[0])
    with pytest.raises(InvalidTaskTransition):
        service(lease, assignment, log, acquisition=[bad]).process(envelope(lease))


@pytest.mark.parametrize(
    "prepare", [CoordinatedRuntimePrepareKind.PREPARED, CoordinatedRuntimePrepareKind.REPLAY]
)
def test_prepare_prepared_or_replay_continues_in_order(prepare):
    lease, assignment, handle = objects()
    log = Log()
    obs = observation(lease, assignment)
    result = service(
        lease,
        assignment,
        log,
        prepare=prepare,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle, obs),
    ).process(envelope(lease))
    assert result.kind.value == "PROCESSED"
    assert log.events == [
        "acquire",
        "assignment",
        "validate",
        "prepare",
        "bind_context",
        "cross",
        "dispatch",
        "bind_receipt",
        "convergence",
    ]


def test_authorized_dispatch_response_loss_is_unknown_once():
    lease, assignment, handle = objects()
    log = Log()
    r = receipt(lease, assignment, handle)
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=r,
        inspected=None,
    )

    def fail(*_a, **_k):
        log.add("dispatch")
        raise RuntimeError("injected")

    svc._adapter.dispatch = fail
    result = svc.process(envelope(lease))
    assert result.kind.value == "PARKED_UNKNOWN"
    assert log.events.count("dispatch") == 1
    assert log.events[-1] == "unknown"


def test_validation_exception_fails_once_and_never_crosses_boundary():
    lease, assignment, _ = objects()
    log = Log()
    svc = service(lease, assignment, log, acquisition=[CoordinatedDeliveryResult.acquired(lease)])

    def explode(_):
        log.add("validate")
        raise RuntimeError("validator")

    svc._adapter.validate = explode
    result = svc.process(envelope(lease))
    assert result.kind.value == "PROCESSED"
    assert log.events == ["acquire", "assignment", "validate", "fail"]


def test_already_crossed_has_no_provider_dispatch():
    lease, assignment, handle = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        crossed=CoordinatedRuntimeDispatchKind.ALREADY_CROSSED,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle, observation(lease, assignment)),
    )
    with pytest.raises(DeliveryInProgress):
        svc.process(envelope(lease))
    assert log.events == ["acquire", "assignment", "validate", "prepare", "bind_context", "cross"]


def test_recover_crossed_missing_handle_zero_dispatch():
    lease, assignment, _ = objects()
    log = Log()
    proof = RecoveryCrossedProof(
        execution_id=lease.runtime_execution_intent_id,
        expired_owner_attempt_id=lease.attempt_id,
        expired_owner_fencing_token=lease.fencing_token,
        phase=RuntimeExecutionPhase.RUNNING,
        version=1,
        assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest,
    )
    result = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.recover_crossed(proof)],
        snapshots=None,
    ).process(envelope(lease))
    assert result.kind.value == "PARKED_UNKNOWN"
    assert "dispatch" not in log.events and "inspect" not in log.events
    assert log.events == ["acquire", "unknown"]


def test_invalid_clock_rejected():
    lease, assignment, _ = objects()
    with pytest.raises(InvalidTaskInput):
        service(lease, assignment, Log(), acquisition=[], clock=lambda: "bad").process(
            envelope(lease)
        )


def test_prepare_drain_reacquires_and_accepts_blocked_result():
    lease, assignment, _ = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        prepare=CoordinatedRuntimePrepareKind.BLOCKED_BY_DRAIN,
        acquisition=[
            CoordinatedDeliveryResult.acquired(lease),
            CoordinatedDeliveryResult.without_lease(
                CoordinatedDeliveryResultKind.BLOCKED_BY_DRAIN, reason="drain"
            ),
        ],
    )
    result = svc.process(envelope(lease))
    assert result.kind.value == "BLOCKED_BY_DRAIN"
    assert log.events == ["acquire", "assignment", "validate", "prepare", "acquire"]


def test_boundary_drain_reacquires_and_accepts_replay():
    lease, assignment, _ = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        crossed=CoordinatedRuntimeDispatchKind.BLOCKED_BY_DRAIN,
        acquisition=[
            CoordinatedDeliveryResult.acquired(lease),
            CoordinatedDeliveryResult.without_lease(CoordinatedDeliveryResultKind.REPLAY_PROCESSED),
        ],
    )
    result = svc.process(envelope(lease))
    assert result.kind.value == "REPLAY"
    assert log.events == [
        "acquire",
        "assignment",
        "validate",
        "prepare",
        "bind_context",
        "cross",
        "acquire",
    ]


def test_assignment_builder_exception_and_context_bind_exception_fail_once():
    lease, assignment, _ = objects()
    log = Log()
    svc = service(lease, assignment, log, acquisition=[CoordinatedDeliveryResult.acquired(lease)])

    def assignment_failure(*_args):
        log.add("assignment")
        raise RuntimeError("builder")

    svc._managed_execution.assignment_for_delivery = assignment_failure
    result = svc.process(envelope(lease))
    assert result.kind.value == "PROCESSED"
    assert log.events == ["acquire", "assignment", "fail"]

    log = Log()
    svc = service(lease, assignment, log, acquisition=[CoordinatedDeliveryResult.acquired(lease)])

    def context_failure(*_args):
        log.add("bind_context")
        raise RuntimeError("bind")

    svc._managed_execution.bind_delivery_context = context_failure
    result = svc.process(envelope(lease))
    assert result.kind.value == "PROCESSED"
    assert log.events == ["acquire", "assignment", "validate", "prepare", "bind_context", "fail"]


def test_handle_only_receipt_binds_before_inspect_and_finalize():
    lease, assignment, handle = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle),
        inspected=observation(lease, assignment),
    )
    result = svc.process(envelope(lease))
    assert result.kind.value == "PROCESSED"
    assert log.events[-3:] == ["bind_receipt", "inspect", "convergence"]


def test_inspection_failure_becomes_synthetic_unknown():
    lease, assignment, handle = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle),
    )

    def inspect_failure(_handle):
        log.add("inspect")
        raise RuntimeError("provider unavailable")

    svc._adapter.inspect = inspect_failure
    result = svc.process(envelope(lease))
    assert result.kind.value == "PARKED_UNKNOWN"
    assert log.events[-2:] == ["inspect", "unknown"]


def test_predispatch_failure_exception_is_propagated_once():
    lease, assignment, _ = objects()
    log = Log()
    svc = service(lease, assignment, log, acquisition=[CoordinatedDeliveryResult.acquired(lease)])

    def fail_delivery(*_args):
        log.add("fail")
        raise RuntimeError("control plane")

    svc._predispatch_failure_service.fail_delivery = fail_delivery
    with pytest.raises(RuntimeError, match="control plane"):
        svc._adapter.validate = lambda _assignment: ValidationReport(valid=False)
        svc.process(envelope(lease))
    assert log.events == ["acquire", "assignment", "fail"]


def test_recovery_valid_snapshot_inspects_without_dispatch():
    lease, assignment, handle = objects()
    snapshot = handle_snapshot_for(handle, tenant_id=lease.tenant_id)
    proof = RecoveryCrossedProof(
        execution_id=lease.runtime_execution_intent_id,
        expired_owner_attempt_id=lease.attempt_id,
        expired_owner_fencing_token=lease.fencing_token,
        phase=RuntimeExecutionPhase.RUNNING,
        version=1,
        assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest,
        persisted_handle_snapshot_id=snapshot.id,
        persisted_handle_snapshot_digest=snapshot.handle_digest,
    )
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.recover_crossed(proof)],
        snapshots=snapshot,
        inspected=observation(lease, assignment),
    )
    result = svc.process(envelope(lease))
    assert result.kind.value == "PROCESSED"
    assert "dispatch" not in log.events
    assert log.events == ["acquire", "inspect", "convergence"]


def test_recovery_corrupt_snapshot_is_unknown_without_inspect_or_dispatch():
    lease, assignment, handle = objects()
    snapshot = handle_snapshot_for(handle, tenant_id=lease.tenant_id)
    proof = RecoveryCrossedProof(
        execution_id=lease.runtime_execution_intent_id,
        expired_owner_attempt_id=lease.attempt_id,
        expired_owner_fencing_token=lease.fencing_token,
        phase=RuntimeExecutionPhase.RUNNING,
        version=1,
        assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest,
        persisted_handle_snapshot_id=snapshot.id,
        persisted_handle_snapshot_digest="0" * 64,
    )
    log = Log()
    result = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.recover_crossed(proof)],
        snapshots=snapshot,
    ).process(envelope(lease))
    assert result.kind.value == "PARKED_UNKNOWN"
    assert log.events == ["acquire", "unknown"]


@pytest.mark.parametrize(
    ("phase", "collaborator", "expected"),
    [
        (RuntimePhase.SUCCEEDED, "convergence", CoordinatedKnownTerminalKind.REPLAY),
        (RuntimePhase.OUTCOME_UNKNOWN, "unknown", CoordinatedUnknownOutcomeKind.REPLAY),
    ],
)
def test_collaborator_replay_is_public_replay(phase, collaborator, expected):
    lease, assignment, handle = objects()
    log = Log()
    result_class = (
        CoordinatedKnownTerminalResult
        if collaborator == "convergence"
        else CoordinatedUnknownOutcomeResult
    )
    kwargs = {
        collaborator: typed_result(
            result_class,
            expected,
            tenant_id=lease.tenant_id,
            task_id=lease.task_id,
            run_id=lease.run_id,
            attempt_id=lease.attempt_id,
            runtime_execution_id=lease.runtime_execution_intent_id,
        )
    }
    result = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle, observation(lease, assignment, phase=phase)),
        **kwargs,
    ).process(envelope(lease))
    assert result.kind.value == "REPLAY"


@pytest.mark.parametrize(
    "collaborator",
    ["convergence", "unknown"],
)
def test_collaborator_invalid_kind_fails_closed(collaborator):
    lease, assignment, handle = objects()
    log = Log()
    result_class = (
        CoordinatedKnownTerminalResult
        if collaborator == "convergence"
        else CoordinatedUnknownOutcomeResult
    )
    result_kwargs = {collaborator: typed_result(result_class, "NOT_A_RESULT")}
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(
            lease,
            assignment,
            handle,
            observation(
                lease,
                assignment,
                phase=(
                    RuntimePhase.SUCCEEDED
                    if collaborator == "convergence"
                    else RuntimePhase.OUTCOME_UNKNOWN
                ),
            ),
        ),
        **result_kwargs,
    )
    with pytest.raises(InvalidTaskTransition):
        svc.process(envelope(lease))


def test_invalid_collaborator_execution_identity_fails_closed():
    lease, assignment, handle = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle, observation(lease, assignment)),
    )
    svc._dispatch.prepare_runtime_assignment = lambda **_kwargs: typed_result(
        CoordinatedRuntimePrepareResult, CoordinatedRuntimePrepareKind.PREPARED, uuid4()
    )
    with pytest.raises(InvalidTaskTransition):
        svc.process(envelope(lease))
    assert "dispatch" not in log.events


def test_fresh_clock_values_produce_monotonic_received_at():
    lease, assignment, handle = objects()
    times = [NOW + timedelta(seconds=index) for index in range(10)]
    observed = observation(lease, assignment, at=NOW + timedelta(seconds=20))
    received = []
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle, observed),
        clock=lambda: times.pop(0),
    )
    original = svc._convergence.apply_delivery_terminal

    def capture(**kwargs):
        received.append(kwargs["received_at"])
        return original(**kwargs)

    svc._convergence.apply_delivery_terminal = capture
    result = svc.process(envelope(lease))
    assert result.kind.value == "PROCESSED"
    assert received == [observed.observed_at]


@pytest.mark.parametrize("malformed", ["assignment", "validation"])
def test_malformed_assignment_or_validation_fails_once_before_prepare(malformed):
    lease, assignment, _ = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
    )
    if malformed == "assignment":
        svc._managed_execution.assignment_for_delivery = lambda *_args: SimpleNamespace(
            malformed=True
        )
    else:
        svc._adapter.validate = lambda _assignment: SimpleNamespace(valid=True)
    result = svc.process(envelope(lease))
    assert result.kind.value == "PROCESSED"
    assert log.events.count("fail") == 1
    assert "prepare" not in log.events
    assert "cross" not in log.events
    assert "dispatch" not in log.events


@pytest.mark.parametrize(
    ("phase", "collaborator"),
    [
        (RuntimePhase.SUCCEEDED, "convergence"),
        (RuntimePhase.OUTCOME_UNKNOWN, "unknown"),
    ],
)
def test_terminal_collaborators_receive_exact_delivery_identity(phase, collaborator):
    lease, assignment, handle = objects()
    message = envelope(lease)
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle, observation(lease, assignment, phase=phase)),
    )

    result = svc.process(message)

    assert result.kind.value in {"PROCESSED", "PARKED_UNKNOWN"}
    target = svc._convergence if collaborator == "convergence" else svc._unknown
    assert len(target.calls) == 1
    assert target.calls[0]["consumer_name"] == "consumer"
    assert target.calls[0]["envelope"] is message
    if collaborator == "unknown":
        assert target.calls[0]["conflict"] is None


def test_dispatch_call_exception_parks_unknown_without_conflict():
    lease, assignment, _handle = objects()
    message = envelope(lease)
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
    )

    def dispatch_failure(*_args, **_kwargs):
        log.add("dispatch")
        raise RuntimeError("provider unavailable")

    svc._adapter.dispatch = dispatch_failure
    result = svc.process(message)

    assert result.kind.value == "PARKED_UNKNOWN"
    assert svc._unknown.calls[0]["conflict"] is None
    assert svc._unknown.calls[0]["envelope"] is message


def test_returned_invalid_dispatch_receipt_parks_bounded_structural_conflict():
    lease, assignment, handle = objects()
    message = envelope(lease)
    log = Log()
    invalid_receipt = receipt(lease, assignment, handle, observation(lease, assignment))
    invalid_receipt.dispatch_key = "runtime-dispatch:wrong"
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=invalid_receipt,
    )

    result = svc.process(message)

    assert result.kind.value == "PARKED_UNKNOWN"
    conflict = svc._unknown.calls[0]["conflict"]
    assert type(conflict) is ManagedRuntimeConflictObservation
    assert conflict.structural_invalid is True
    assert conflict.terminal_contract_invalid is True
    assert svc._unknown.calls[0]["observation"].phase is RuntimePhase.OUTCOME_UNKNOWN
    assert log.events.count("unknown") == 1
    assert "convergence" not in log.events


def test_returned_dispatch_observation_identity_conflict_retains_bounded_flags():
    lease, assignment, handle = objects()
    contradictory = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(lease.runtime_execution_intent_id),
        assignment_id=str(uuid4()),
        assignment_digest=assignment.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=NOW,
        provider_event_id="dispatch-assignment-conflict",
        output={},
    )
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle, contradictory),
    )

    assert svc.process(envelope(lease)).kind.value == "PARKED_UNKNOWN"
    conflict = svc._unknown.calls[0]["conflict"]
    assert conflict.assignment_id_mismatch is True
    assert conflict.structural_invalid is False
    assert svc._unknown.calls[0]["observation"].phase is RuntimePhase.OUTCOME_UNKNOWN


def _protocol_error_observation(lease, assignment):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(lease.runtime_execution_intent_id),
        assignment_id=assignment.assignment_id,
        assignment_digest=assignment.assignment_digest,
        phase=RuntimePhase.FAILED,
        observed_at=NOW,
        provider_event_id="protocol-error-event",
        error=RuntimeErrorDTO(
            code="runtime.protocol_error",
            category=ErrorCategory.PERMANENT,
            message="bounded protocol failure",
            retry_disposition=RetryDisposition.NEVER,
        ),
    )


@pytest.mark.parametrize(
    ("candidate_factory", "flag"),
    [
        (lambda _lease, _assignment: SimpleNamespace(raw="malformed"), "structural_invalid"),
        (
            lambda lease, assignment: RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=str(uuid4()),
                assignment_id=assignment.assignment_id,
                assignment_digest=assignment.assignment_digest,
                phase=RuntimePhase.SUCCEEDED,
                observed_at=NOW,
                provider_event_id="wrong-execution",
                output={},
            ),
            "execution_id_mismatch",
        ),
        (
            lambda lease, assignment: RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=str(lease.runtime_execution_intent_id),
                assignment_id=str(uuid4()),
                assignment_digest=assignment.assignment_digest,
                phase=RuntimePhase.SUCCEEDED,
                observed_at=NOW,
                provider_event_id="wrong-assignment",
                output={},
            ),
            "assignment_id_mismatch",
        ),
        (
            lambda lease, assignment: RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=str(lease.runtime_execution_intent_id),
                assignment_id=assignment.assignment_id,
                assignment_digest=assignment.assignment_digest,
                phase=RuntimePhase.RUNNING,
                observed_at=NOW,
                provider_event_id="nonterminal",
            ),
            "terminal_contract_invalid",
        ),
        (
            lambda lease, assignment: RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=str(lease.runtime_execution_intent_id),
                assignment_id=assignment.assignment_id,
                assignment_digest=assignment.assignment_digest,
                phase=RuntimePhase.SUCCEEDED,
                observed_at=NOW,
                provider_event_id="invalid-output",
                output=["not", "a", "mapping"],
            ),
            "terminal_contract_invalid",
        ),
        (
            lambda lease, assignment: RuntimeObservation(
                observation_id=str(uuid4()),
                runtime_execution_id=str(lease.runtime_execution_intent_id),
                assignment_id=assignment.assignment_id,
                assignment_digest=assignment.assignment_digest,
                phase=RuntimePhase.SUCCEEDED,
                observed_at=NOW,
                provider_event_id="invalid-usage",
                output={},
                usage={"input_tokens": 1},
            ),
            "terminal_contract_invalid",
        ),
        (_protocol_error_observation, "protocol_error_observation"),
    ],
)
def test_returned_inspect_contradiction_is_bounded_conflict(candidate_factory, flag):
    lease, assignment, handle = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle),
        inspected=candidate_factory(lease, assignment),
    )

    result = svc.process(envelope(lease))

    assert result.kind.value == "PARKED_UNKNOWN"
    conflict = svc._unknown.calls[0]["conflict"]
    assert type(conflict) is ManagedRuntimeConflictObservation
    assert getattr(conflict, flag) is True
    assert svc._unknown.calls[0]["observation"].phase is RuntimePhase.OUTCOME_UNKNOWN
    assert "convergence" not in log.events


def test_inspect_call_exception_has_no_conflict_marker():
    lease, assignment, handle = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle),
    )

    def inspect_failure(_handle):
        log.add("inspect")
        raise RuntimeError("provider call failed")

    svc._adapter.inspect = inspect_failure
    result = svc.process(envelope(lease))

    assert result.kind.value == "PARKED_UNKNOWN"
    assert svc._unknown.calls[0]["conflict"] is None


def test_recovery_terminal_and_unknown_use_delivery_collaborators():
    lease, assignment, handle = objects()
    snapshot = handle_snapshot_for(handle, tenant_id=lease.tenant_id)
    proof = RecoveryCrossedProof(
        execution_id=lease.runtime_execution_intent_id,
        expired_owner_attempt_id=lease.attempt_id,
        expired_owner_fencing_token=lease.fencing_token,
        phase=RuntimeExecutionPhase.RUNNING,
        version=1,
        assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest,
        persisted_handle_snapshot_id=snapshot.id,
        persisted_handle_snapshot_digest=snapshot.handle_digest,
    )
    message = envelope(lease)
    log = Log()
    known = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.recover_crossed(proof)],
        snapshots=snapshot,
        inspected=observation(lease, assignment),
    )
    assert known.process(message).kind.value == "PROCESSED"
    assert known._convergence.calls[0]["consumer_name"] == "consumer"
    assert known._convergence.calls[0]["envelope"] is message

    log = Log()
    unknown = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.recover_crossed(proof)],
        snapshots=None,
    )
    assert unknown.process(message).kind.value == "PARKED_UNKNOWN"
    assert unknown._unknown.calls[0]["consumer_name"] == "consumer"
    assert unknown._unknown.calls[0]["envelope"] is message
    assert unknown._unknown.calls[0]["conflict"] is None


def test_recovery_returned_contradiction_parks_bounded_conflict():
    lease, assignment, handle = objects()
    snapshot = handle_snapshot_for(handle, tenant_id=lease.tenant_id)
    proof = RecoveryCrossedProof(
        execution_id=lease.runtime_execution_intent_id,
        expired_owner_attempt_id=lease.attempt_id,
        expired_owner_fencing_token=lease.fencing_token,
        phase=RuntimeExecutionPhase.RUNNING,
        version=1,
        assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest,
        persisted_handle_snapshot_id=snapshot.id,
        persisted_handle_snapshot_digest=snapshot.handle_digest,
    )
    contradictory = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(lease.runtime_execution_intent_id),
        assignment_id=str(uuid4()),
        assignment_digest=assignment.assignment_digest,
        phase=RuntimePhase.SUCCEEDED,
        observed_at=NOW,
        provider_event_id="recovery-assignment-conflict",
        output={},
    )
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.recover_crossed(proof)],
        snapshots=snapshot,
        inspected=contradictory,
    )

    assert svc.process(envelope(lease)).kind.value == "PARKED_UNKNOWN"
    conflict = svc._unknown.calls[0]["conflict"]
    assert conflict.assignment_id_mismatch is True
    assert svc._unknown.calls[0]["observation"].phase is RuntimePhase.OUTCOME_UNKNOWN
    assert svc._convergence.calls == []


def test_terminal_validation_application_defect_is_not_normalized(monkeypatch):
    lease, assignment, handle = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle, observation(lease, assignment)),
    )

    def application_defect(*_args, **_kwargs):
        raise RuntimeError("application defect")

    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_service."
        "validate_terminal_observation",
        application_defect,
    )
    with pytest.raises(RuntimeError, match="application defect"):
        svc.process(envelope(lease))
    assert svc._unknown.calls == []
    assert svc._convergence.calls == []


def test_dispatch_normalizer_application_defect_is_not_normalized(monkeypatch):
    lease, assignment, handle = objects()
    log = Log()
    svc = service(
        lease,
        assignment,
        log,
        acquisition=[CoordinatedDeliveryResult.acquired(lease)],
        receipt=receipt(lease, assignment, handle, observation(lease, assignment)),
    )

    def application_defect(*_args, **_kwargs):
        raise RuntimeError("normalizer defect")

    monkeypatch.setattr(
        "agentmesh.application.coordinated_runtime_delivery_service."
        "normalize_dispatch_receipt",
        application_defect,
    )
    with pytest.raises(RuntimeError, match="normalizer defect"):
        svc.process(envelope(lease))
    assert svc._unknown.calls == []
    assert svc._convergence.calls == []
