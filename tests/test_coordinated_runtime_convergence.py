from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.business_outcomes import KnownTerminalPhase
from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierCompletion,
    plan_known_terminal,
)
from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedKnownTerminalKind,
    CoordinatedKnownTerminalResult,
    _classify_replay,
    _replay_drain_projection,
    _safe_error,
    _select_target,
    _validate_cancel_projection,
    _validate_command,
)
from agentmesh.application.runtime_contracts import TerminalObservationValidator
from agentmesh.application.runtime_services import classify_locked_observation
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrainTarget,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
)
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
)
from agentmesh.domain.tasks import (
    RunRole,
    RunStatus,
    TaskStatus,
)
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from tests.test_coordinated_runtime_barrier import (
    _aggregate,
    _aggregate_for_sibling_boundaries,
    _cancel_intent,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _observation(execution, *, phase=RuntimePhase.SUCCEEDED, at=NOW, **changes):
    values = {
        "observation_id": str(uuid4()),
        "runtime_execution_id": str(execution.id),
        "assignment_id": str(execution.assignment_id),
        "assignment_digest": execution.assignment_digest,
        "phase": phase,
        "observed_at": at,
        "provider_event_id": "provider-terminal",
        "provider_sequence": 1,
        "output": {"ok": True} if phase is RuntimePhase.SUCCEEDED else None,
    }
    values.update(changes)
    return RuntimeObservation(**values)


def _evidence(observation, execution, *, received_at=NOW, **changes):
    values = {
        "id": uuid4(),
        "tenant_id": "tenant-a",
        "runtime_execution_id": execution.id,
        "observation_id": observation.observation_id,
        "observation_digest": TerminalObservationValidator.digest(observation),
        "assignment_id": execution.assignment_id,
        "assignment_digest": execution.assignment_digest,
        "provider_sequence": observation.provider_sequence,
        "phase": RuntimeExecutionPhase(observation.phase.value),
        "observed_at": observation.observed_at,
        "received_at": received_at,
        "safe_summary": None,
        "processing_outcome": RuntimeObservationOutcome.APPLIED,
        "provider_event_present": True,
        "evidence": MappingProxyType({"provider_event_id": observation.provider_event_id}),
    }
    values.update(changes)
    return RuntimeObservationEvidence(**values)


def _read_only_uow():
    return SimpleNamespace(
        coordination_runtime_drains=SimpleNamespace(
            get=lambda *_args, **_kwargs: None,
        )
    )


def test_result_vocabulary_requires_closed_digest_and_ordered_schedule() -> None:
    ids = tuple(sorted((uuid4(), uuid4()), key=str))
    result = CoordinatedKnownTerminalResult(
        kind=CoordinatedKnownTerminalKind.APPLIED,
        tenant_id="tenant-a",
        task_id=uuid4(),
        run_id=uuid4(),
        attempt_id=uuid4(),
        runtime_execution_id=uuid4(),
        observation_id="observation-1",
        observation_digest="a" * 64,
        task_status=TaskStatus.RUNNING,
        run_status=RunStatus.SUCCEEDED,
        subtask_status=SubtaskStatus.COMPLETED,
        scheduled_run_ids=ids,
    )
    assert result.scheduled_run_ids == ids
    with pytest.raises(RuntimeExecutionConflict, match="observation"):
        replace(result, observation_digest="A" * 64)
    with pytest.raises(RuntimeExecutionConflict, match="ordered"):
        replace(result, scheduled_run_ids=tuple(reversed(ids)))
    with pytest.raises(RuntimeExecutionConflict, match="target"):
        replace(result, drain_target=object())


@pytest.mark.parametrize(
    ("phase", "requested", "expected"),
    [
        (RuntimePhase.SUCCEEDED, False, None),
        (RuntimePhase.FAILED, False, "runtime.failed"),
        (RuntimePhase.TIMED_OUT, False, "runtime.timed_out"),
        (RuntimePhase.CANCELED, False, "runtime.unrequested_cancellation"),
        (RuntimePhase.CANCELED, True, "runtime.canceled"),
    ],
)
def test_safe_error_is_closed_and_cancel_aware(phase, requested, expected) -> None:
    execution = _aggregate()[1][3]
    observation = _observation(execution, phase=phase)
    assert _safe_error(observation, cancel_intent_present=requested) == expected


@pytest.mark.parametrize(
    ("phase", "completion", "target"),
    [
        (KnownTerminalPhase.SUCCEEDED, CoordinatedBarrierCompletion.CONTINUE_SUCCESS, None),
        (
            KnownTerminalPhase.FAILED,
            CoordinatedBarrierCompletion.APPLY_FAILED,
            CoordinationRuntimeDrainTarget.FAILED,
        ),
        (
            KnownTerminalPhase.TIMED_OUT,
            CoordinatedBarrierCompletion.APPLY_FAILED,
            CoordinationRuntimeDrainTarget.FAILED,
        ),
        (
            KnownTerminalPhase.CANCELED,
            CoordinatedBarrierCompletion.APPLY_FAILED,
            CoordinationRuntimeDrainTarget.FAILED,
        ),
    ],
)
def test_known_terminal_phase_matrix_is_closed(phase, completion, target) -> None:
    _task, target_chain, aggregate = _aggregate()
    plan = plan_known_terminal(
        aggregate,
        triggering_run_id=target_chain[1].id,
        phase=phase,
        cancel_intent_present=False,
        safe_error=None,
    )
    assert plan.completion is completion
    assert plan.effective_target is target


def test_canceled_with_intent_without_preexisting_drain_is_rejected() -> None:
    _task, target, aggregate = _aggregate()
    intent = _cancel_intent(tenant_id="tenant-a", execution_id=target[3].id)
    aggregate = replace(aggregate, lifecycle_operations=(intent,))
    with pytest.raises(RuntimeExecutionConflict, match="cancel"):
        plan_known_terminal(
            aggregate,
            triggering_run_id=target[1].id,
            phase=KnownTerminalPhase.CANCELED,
            cancel_intent_present=True,
            safe_error=None,
        )


@pytest.mark.parametrize(
    "received_at",
    [
        NOW.replace(tzinfo=None),
        NOW.astimezone(timezone(timedelta(hours=8))),
    ],
)
def test_command_rejects_non_utc_or_invalid_receipt_clock(received_at) -> None:
    execution = _aggregate()[1][3]
    observation = _observation(execution)
    with pytest.raises((InvalidTaskInput, InvalidTaskTransition)):
        _validate_command(
            tenant_id="tenant-a",
            task_id=uuid4(),
            run_id=uuid4(),
            attempt_id=uuid4(),
            fencing_token=1,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=received_at,
            causation_id=uuid4(),
        )


def test_replay_no_drain_allows_crossed_sibling_but_not_reconciliation_projection() -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,)
    )
    assert (
        _replay_drain_projection(
            _read_only_uow(),
            aggregate=aggregate,
            phase=RuntimePhase.SUCCEEDED,
            cancel_intent_present=False,
            safe_error=None,
            current_run_id=target[1].id,
            accepted_received_at=NOW,
        )
        is None
    )
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,)
    )
    with pytest.raises(RuntimeExecutionConflict, match="reconciliation"):
        _replay_drain_projection(
            _read_only_uow(),
            aggregate=aggregate,
            phase=RuntimePhase.SUCCEEDED,
            cancel_intent_present=False,
            safe_error=None,
            current_run_id=target[1].id,
            accepted_received_at=NOW,
        )


def test_replay_no_drain_requires_clean_running_task_projection() -> None:
    task, target, aggregate = _aggregate()
    for field, value in (
        ("output", {"late": True}),
        ("error", "runtime.failed"),
        ("current_run_id", uuid4()),
    ):
        dirty = replace(task, **{field: value})
        dirty_aggregate = replace(aggregate, task=dirty)
        with pytest.raises(RuntimeExecutionConflict, match="Task status"):
            _replay_drain_projection(
                    _read_only_uow(),
                aggregate=dirty_aggregate,
                phase=RuntimePhase.SUCCEEDED,
                cancel_intent_present=False,
                safe_error=None,
                current_run_id=target[1].id,
                accepted_received_at=NOW,
            )


@pytest.mark.parametrize("field", ["safe_summary", "provider_event_present", "evidence"])
def test_replay_evidence_requires_full_safe_projection(field) -> None:
    task, target, aggregate = _aggregate()
    execution = target[3]
    observation = _observation(execution)
    evidence = _evidence(observation, execution)
    bad = {
        "safe_summary": "forged",
        "provider_event_present": False,
        "evidence": MappingProxyType({"forged": True}),
    }[field]
    evidence = replace(evidence, **{field: bad})
    with pytest.raises(RuntimeExecutionConflict, match="evidence projection"):
        _classify_replay(
            [evidence],
            [evidence],
            uow=SimpleNamespace(),
            aggregate=aggregate,
            run=target[1],
            attempt=target[2],
            subtask=target[0],
            observation=observation,
            observation_digest=evidence.observation_digest,
            execution=execution,
            phase=__import__(
                "agentmesh.application.business_outcomes", fromlist=["KnownTerminalPhase"]
            ).KnownTerminalPhase.SUCCEEDED,
            safe_error=None,
            cancel_intent_present=False,
            received_at=NOW,
        )


@pytest.mark.parametrize("field", ["observed_at", "received_at"])
def test_replay_evidence_timestamps_must_be_utc(field) -> None:
    _task, target, aggregate = _aggregate()
    observation = _observation(target[3])
    evidence = _evidence(
        observation,
        target[3],
        **{field: NOW.astimezone(timezone(timedelta(hours=8)))},
    )
    with pytest.raises(RuntimeExecutionConflict, match="evidence projection"):
        _classify_replay(
            [evidence],
            [evidence],
            uow=SimpleNamespace(),
            aggregate=aggregate,
            run=target[1],
            attempt=target[2],
            subtask=target[0],
            observation=observation,
            observation_digest=evidence.observation_digest,
            execution=target[3],
            phase=__import__(
                "agentmesh.application.business_outcomes", fromlist=["KnownTerminalPhase"]
            ).KnownTerminalPhase.SUCCEEDED,
            safe_error=None,
            cancel_intent_present=False,
            received_at=NOW,
        )


def test_replay_rejects_multiple_or_non_applied_anchors() -> None:
    _task, target, aggregate = _aggregate()
    observation = _observation(target[3])
    first = _evidence(observation, target[3])
    second = replace(first, id=uuid4())
    kwargs = {
        "uow": _read_only_uow(),
        "aggregate": aggregate,
        "run": target[1],
        "attempt": target[2],
        "subtask": target[0],
        "observation": observation,
        "observation_digest": first.observation_digest,
        "execution": target[3],
        "phase": KnownTerminalPhase.SUCCEEDED,
        "safe_error": None,
        "cancel_intent_present": False,
        "received_at": NOW,
    }
    with pytest.raises(RuntimeExecutionConflict, match="ambiguous"):
        _classify_replay([first, second], [first], **kwargs)
    non_applied = replace(first, processing_outcome=RuntimeObservationOutcome.DUPLICATE)
    with pytest.raises(RuntimeExecutionConflict, match="ambiguous"):
        _classify_replay([non_applied], [non_applied], **kwargs)


def test_cancel_projection_only_accepts_exact_stable_target_intent() -> None:
    _task, target, aggregate = _aggregate()
    exact = _cancel_intent(tenant_id="tenant-a", execution_id=target[3].id)
    assert _validate_cancel_projection(
        replace(aggregate, lifecycle_operations=(exact,)), target[3].id, "tenant-a"
    )
    wrong = replace(exact, operation_id=f"runtime-cancel:{uuid4()}:v1")
    with pytest.raises(RuntimeExecutionConflict, match="cancel evidence"):
        _validate_cancel_projection(
            replace(aggregate, lifecycle_operations=(wrong,)), target[3].id, "tenant-a"
        )


@pytest.mark.parametrize(
    ("label", "mutator"),
    [
        (
            "task",
            lambda aggregate, target: replace(
                aggregate, task=replace(aggregate.task, id=uuid4())
            ),
        ),
        (
            "run",
            lambda aggregate, target: replace(
                aggregate, runs=(replace(target[1], id=uuid4()),)
            ),
        ),
        (
            "subtask",
            lambda aggregate, target: replace(
                aggregate, subtasks=(replace(target[0], task_id=uuid4()),)
            ),
        ),
        (
            "duplicate",
            lambda aggregate, target: replace(
                aggregate, runs=aggregate.runs + (target[1],)
            ),
        ),
        (
            "role",
            lambda aggregate, target: replace(
                aggregate, runs=(replace(target[1], role=RunRole.REVIEWER),)
            ),
        ),
    ],
)
def test_target_identity_and_role_mismatches_fail_closed(label, mutator) -> None:
    _task, target, aggregate = _aggregate()
    broken = mutator(aggregate, target)
    with pytest.raises((RuntimeExecutionConflict, InvalidTaskTransition), match="Known-terminal"):
        _select_target(
            broken,
            tenant_id="tenant-a",
            task_id=target[1].task_id,
            run_id=target[1].id,
            attempt_id=target[2].id,
            fencing_token=target[2].fencing_token,
            runtime_execution_id=target[3].id,
            received_at=NOW,
        )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("applied", RuntimeObservationOutcome.APPLIED),
        ("duplicate", RuntimeObservationOutcome.DUPLICATE),
        ("conflict", RuntimeObservationOutcome.CONFLICT),
        ("stale", RuntimeObservationOutcome.STALE_OWNER),
        ("gap", RuntimeObservationOutcome.GAP),
    ],
)
def test_locked_observation_classifier_matches_registry_vocabulary(name, expected) -> None:
    _task, target, _locked_aggregate = _aggregate()
    execution = target[3]
    observation_id = "observation-1"
    digest = "a" * 64
    prior = []
    assignment_id = execution.assignment_id
    assignment_digest = execution.assignment_digest
    attempt_id = target[2].id
    fencing_token = target[2].fencing_token
    provider_sequence = 2
    if name == "duplicate":
        prior = [_evidence(_observation(execution), execution)]
    elif name == "conflict":
        assignment_id = uuid4()
    elif name == "stale":
        attempt_id = uuid4()
    elif name == "gap":
        execution = execution.apply_observation(
            phase=RuntimeExecutionPhase.RUNNING,
            provider_sequence=1,
            now=NOW,
        )
        provider_sequence = 3
    elif name == "applied":
        provider_sequence = 2
    actual = classify_locked_observation(
        execution,
        prior=prior,
        assignment_id=assignment_id,
        assignment_digest=assignment_digest,
        provider_sequence=provider_sequence,
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        observation_id=observation_id,
        observation_digest=digest,
    )
    assert actual is expected


def test_apply_completion_defer_mode_leaves_caller_one_task_save() -> None:
    from agentmesh.application.coordinated_runtime_convergence import _apply_completion

    task, _target, aggregate = _aggregate(drain_target=None)
    at = max(NOW, task.updated_at)
    drain = __import__(
        "agentmesh.domain.coordination", fromlist=["CoordinationRuntimeDrain"]
    ).CoordinationRuntimeDrain.start(
        drain_id=uuid4(),
        tenant_id=task.tenant_id,
        task_id=task.id,
        triggering_run_id=aggregate.runs[0].id,
        target=__import__(
            "agentmesh.domain.coordination", fromlist=["CoordinationRuntimeDrainTarget"]
        ).CoordinationRuntimeDrainTarget.FAILED,
        reason="runtime.failed",
        at=at,
    )
    saves = []
    uow = SimpleNamespace(
        tasks=SimpleNamespace(save=lambda value: saves.append(value)),
        coordination_runtime_drains=SimpleNamespace(save=lambda *_args, **_kwargs: None),
    )
    barrier = SimpleNamespace(
        completion=CoordinatedBarrierCompletion.APPLY_FAILED,
        effective_drain=drain,
    )
    assert _apply_completion(
        uow,
        aggregate,
        barrier,
        before_status=TaskStatus.RUNNING,
        at=at,
        defer_task_save=True,
    )
    assert saves == []
    saves.append(aggregate.task)
    assert len(saves) == 1


def test_convergence_ast_guards_have_no_external_callers_or_forbidden_wiring() -> None:
    root = Path(__file__).parents[1] / "src" / "agentmesh"
    convergence = root / "application" / "coordinated_runtime_convergence.py"

    def calls(tree, names):
        return [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name) and node.func.id in names)
                 or (isinstance(node.func, ast.Attribute) and node.func.attr in names))
        ]

    fixture = ast.parse("apply_known_terminal(x)\nservice.apply_known_terminal(x)")
    assert len(calls(fixture, {"apply_known_terminal"})) == 2
    external = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if path != convergence:
            external.extend(calls(tree, {"apply_known_terminal"}))
    assert external == []
    module = ast.parse(convergence.read_text(encoding="utf-8"), filename=str(convergence))
    forbidden_imports = {
        alias.name.split(".")[0]
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden_imports.update(
        node.module.split(".")[0]
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert not forbidden_imports.intersection({"adapter", "worker", "admission"})
    assert not calls(module, {"dispatch", "inspect", "validate"})


def test_convergence_is_one_uow_one_commit_and_locker_first() -> None:
    convergence = Path(__file__).parents[1] / "src" / "agentmesh" / "application" / (
        "coordinated_runtime_convergence.py"
    )
    module = ast.parse(convergence.read_text(encoding="utf-8"), filename=str(convergence))
    method = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "apply_known_terminal"
    )
    uow_factory_calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_uow_factory"
    ]
    commit_calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
    ]
    scheduler_calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "schedule"
    ]
    locker_calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "lock"
    ]
    direct_uow_reads = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "uow"
    ]
    assert len(uow_factory_calls) == 1
    assert len(commit_calls) == 1
    assert len(scheduler_calls) == 1
    assert len(locker_calls) == 1
    assert locker_calls[0].lineno < min(node.lineno for node in direct_uow_reads)


def test_service_result_digest_is_canonical_lowercase_hex() -> None:
    with pytest.raises(RuntimeExecutionConflict):
        CoordinatedKnownTerminalResult(
            kind=CoordinatedKnownTerminalKind.REPLAY,
            tenant_id="tenant-a",
            task_id=uuid4(),
            run_id=uuid4(),
            attempt_id=uuid4(),
            runtime_execution_id=uuid4(),
            observation_id="observation",
            observation_digest="f" * 63 + "g",
            task_status=TaskStatus.RUNNING,
            run_status=RunStatus.RUNNING,
            subtask_status=SubtaskStatus.RUNNING,
        )
