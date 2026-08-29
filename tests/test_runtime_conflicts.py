from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agentmesh.application.ports import ManagedRuntimeConflictObservation
from agentmesh.application.runtime_conflicts import (
    build_managed_runtime_conflict_observation,
)
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.errors import InvalidTaskTransition, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
)
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _execution(*, terminal: bool = False) -> tuple[RuntimeExecution, object, object]:
    attempt_id = uuid4()
    execution = RuntimeExecution.prepare(
        tenant_id="tenant-a",
        run_id=uuid4(),
        runtime_version_id=uuid4(),
        assignment_id=uuid4(),
        assignment_digest="a" * 64,
        dispatch_key="runtime-dispatch:test",
        dispatch_digest="b" * 64,
        now=NOW,
    ).claim(
        attempt_id=attempt_id,
        fencing_token=3,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
        now=NOW,
    )
    if terminal:
        execution = execution.apply_observation(
            phase=RuntimeExecutionPhase.SUCCEEDED,
            provider_sequence=1,
            now=NOW,
        )
    return execution, attempt_id, 3


def _candidate(execution: RuntimeExecution, **changes) -> RuntimeObservation:
    values = {
        "observation_id": str(uuid4()),
        "runtime_execution_id": str(execution.id),
        "assignment_id": str(execution.assignment_id),
        "assignment_digest": execution.assignment_digest,
        "phase": RuntimePhase.FAILED,
        "observed_at": NOW,
        "provider_event_id": "provider-event",
    }
    values.update(changes)
    return RuntimeObservation(**values)


def test_builder_keeps_only_digest_and_boolean_mismatch_flags():
    execution, _, _ = _execution()
    candidate = _candidate(
        execution,
        runtime_execution_id=str(uuid4()),
        assignment_id=str(uuid4()),
        assignment_digest="c" * 64,
        output=None,
    )
    envelope = build_managed_runtime_conflict_observation(
        candidate,
        expected_execution_id=execution.id,
        expected_assignment_id=execution.assignment_id,
        expected_assignment_digest=execution.assignment_digest,
        fallback_observed_at=NOW,
    )
    assert envelope.structural_invalid is False
    assert envelope.terminal_contract_invalid is True
    assert envelope.execution_id_mismatch is True
    assert envelope.assignment_id_mismatch is True
    assert envelope.assignment_digest_mismatch is True
    assert envelope.observation_digest == canonical_digest(candidate.to_dict())
    assert set(vars(envelope)) == {
        "observation_id",
        "observation_digest",
        "phase",
        "observed_at",
        "provider_sequence",
        "structural_invalid",
        "execution_id_mismatch",
        "assignment_id_mismatch",
        "assignment_digest_mismatch",
        "terminal_contract_invalid",
        "protocol_error_observation",
    }


@pytest.mark.parametrize(
    "candidate",
    [object(), None, {"provider_body": "must not survive"}],
)
def test_builder_uses_deterministic_static_marker_for_structural_invalid(candidate):
    execution, _, _ = _execution()
    first = build_managed_runtime_conflict_observation(
        candidate,
        expected_execution_id=execution.id,
        expected_assignment_id=execution.assignment_id,
        expected_assignment_digest=execution.assignment_digest,
        fallback_observed_at=NOW,
    )
    second = build_managed_runtime_conflict_observation(
        candidate,
        expected_execution_id=execution.id,
        expected_assignment_id=execution.assignment_id,
        expected_assignment_digest=execution.assignment_digest,
        fallback_observed_at=NOW,
    )
    assert first == second
    assert first.phase is RuntimePhase.OUTCOME_UNKNOWN
    assert first.observed_at == NOW
    assert first.provider_sequence is None
    assert first.structural_invalid is True
    assert first.terminal_contract_invalid is True


def test_builder_oversize_runtime_observation_does_not_retain_fields():
    execution, _, _ = _execution()
    candidate = _candidate(execution)
    object.__setattr__(candidate, "extensions", {"oversize": "x" * 70_000})
    envelope = build_managed_runtime_conflict_observation(
        candidate,
        expected_execution_id=execution.id,
        expected_assignment_id=execution.assignment_id,
        expected_assignment_digest=execution.assignment_digest,
        fallback_observed_at=NOW,
    )
    assert envelope.structural_invalid is True
    assert envelope.phase is RuntimePhase.OUTCOME_UNKNOWN


def test_builder_unserializable_runtime_observation_uses_static_marker():
    execution, _, _ = _execution()
    candidate = _candidate(execution)
    object.__setattr__(candidate, "extensions", {"bad": "\ud800"})
    envelope = build_managed_runtime_conflict_observation(
        candidate,
        expected_execution_id=execution.id,
        expected_assignment_id=execution.assignment_id,
        expected_assignment_digest=execution.assignment_digest,
        fallback_observed_at=NOW,
    )
    assert envelope.structural_invalid is True
    assert envelope.observation_digest == build_managed_runtime_conflict_observation(
        object(),
        expected_execution_id=execution.id,
        expected_assignment_id=execution.assignment_id,
        expected_assignment_digest=execution.assignment_digest,
        fallback_observed_at=NOW,
    ).observation_digest


class _RuntimeRepo:
    def __init__(self, execution):
        self.execution = execution
        self.observations: list[RuntimeObservationEvidence] = []

    def get_execution(self, execution_id, *, tenant_id, for_update=False):
        if execution_id == self.execution.id and tenant_id == self.execution.tenant_id:
            return self.execution
        return None

    def prior_observations(self, execution_id, *, tenant_id, observation_id, digest):
        return [
            value
            for value in self.observations
            if value.runtime_execution_id == execution_id
            and value.tenant_id == tenant_id
            and (value.observation_id == observation_id or value.observation_digest == digest)
        ]

    def add_observation(self, value):
        self.observations.append(value)

    def save_execution(self, *args, **kwargs):
        raise AssertionError("conflict writer must not mutate RuntimeExecution")


class _Uow:
    def __init__(self, runtime):
        self.runtimes = runtime
        self.committed = False

    def commit(self):
        self.committed = True


def _writer(execution=None):
    execution, attempt_id, fence = execution or _execution()
    repo = _RuntimeRepo(execution)
    uow = _Uow(repo)
    service = RuntimeRegistryService(
        uow_factory=lambda: uow,
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
    )
    return service, uow, execution, attempt_id, fence


def _envelope(execution, *, digest_suffix="c", **flags):
    candidate = _candidate(execution)
    if digest_suffix != "c":
        candidate = _candidate(execution, provider_event_id=f"event-{digest_suffix}")
    envelope = build_managed_runtime_conflict_observation(
        candidate,
        expected_execution_id=execution.id,
        expected_assignment_id=execution.assignment_id,
        expected_assignment_digest=execution.assignment_digest,
        fallback_observed_at=NOW,
    )
    for name, value in flags.items():
        object.__setattr__(envelope, name, value)
    return envelope


def test_conflict_writer_is_immutable_and_exactly_replayable():
    service, uow, execution, attempt_id, fence = _writer()
    envelope = _envelope(execution)
    first = service.record_conflicting_observation_in_uow(
        uow,
        execution_id=execution.id,
        attempt_id=attempt_id,
        fencing_token=fence,
        observation=envelope,
        now=NOW,
    )
    replay = service.record_conflicting_observation_in_uow(
        uow,
        execution_id=execution.id,
        attempt_id=attempt_id,
        fencing_token=fence,
        observation=envelope,
        now=NOW + timedelta(seconds=1),
    )
    assert replay == first
    assert len(uow.runtimes.observations) == 1
    assert execution.phase is RuntimeExecutionPhase.PREPARED
    assert first.processing_outcome is RuntimeObservationOutcome.CONFLICT
    assert set(first.evidence) == {
        "execution_id_mismatch",
        "assignment_id_mismatch",
        "assignment_digest_mismatch",
        "structural_invalid",
        "terminal_contract_invalid",
        "protocol_error_observation",
    }
    assert uow.committed is False


def test_conflict_writer_different_digest_and_collision_fail_closed():
    service, uow, execution, attempt_id, fence = _writer()
    first = _envelope(execution)
    service.record_conflicting_observation_in_uow(
        uow,
        execution_id=execution.id,
        attempt_id=attempt_id,
        fencing_token=fence,
        observation=first,
        now=NOW,
    )
    second = _envelope(execution, digest_suffix="different")
    assert second.observation_digest != first.observation_digest
    service.record_conflicting_observation_in_uow(
        uow,
        execution_id=execution.id,
        attempt_id=attempt_id,
        fencing_token=fence,
        observation=second,
        now=NOW,
    )
    assert len(uow.runtimes.observations) == 2
    collision = ManagedRuntimeConflictObservation(
        **{**vars(first), "terminal_contract_invalid": not first.terminal_contract_invalid}
    )
    with pytest.raises(RuntimeExecutionConflict):
        service.record_conflicting_observation_in_uow(
            uow,
            execution_id=execution.id,
            attempt_id=attempt_id,
            fencing_token=fence,
            observation=collision,
            now=NOW,
        )


def test_conflict_writer_rejects_stale_owner_and_new_marker_after_terminal():
    service, uow, execution, attempt_id, fence = _writer()
    with pytest.raises(RuntimeExecutionConflict):
        service.record_conflicting_observation_in_uow(
            uow,
            execution_id=execution.id,
            attempt_id=uuid4(),
            fencing_token=fence,
            observation=_envelope(execution),
            now=NOW,
        )
    terminal_service, terminal_uow, terminal, terminal_attempt, terminal_fence = _writer(
        _execution(terminal=True)
    )
    with pytest.raises(InvalidTaskTransition):
        terminal_service.record_conflicting_observation_in_uow(
            terminal_uow,
            execution_id=terminal.id,
            attempt_id=terminal_attempt,
            fencing_token=terminal_fence,
            observation=_envelope(terminal, digest_suffix="terminal"),
            now=NOW,
        )
