from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentmesh.application.ports import LateTerminalObservationResultKind
from agentmesh.application.runtime_services import RuntimeRegistryService
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
)
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeExecutionPhase,
    RuntimeIntegrityIncident,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
)
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _terminal_execution() -> RuntimeExecution:
    attempt_id = uuid4()
    return (
        RuntimeExecution.prepare(
            tenant_id="tenant-a",
            run_id=uuid4(),
            runtime_version_id=uuid4(),
            assignment_id=uuid4(),
            assignment_digest="a" * 64,
            dispatch_key="runtime-dispatch:late-terminal",
            dispatch_digest="b" * 64,
            now=NOW,
        )
        .claim(
            attempt_id=attempt_id,
            fencing_token=1,
            expected_owner_attempt_id=None,
            expected_fencing_token=None,
            expected_version=1,
            now=NOW,
        )
        .apply_observation(
            phase=RuntimeExecutionPhase.SUCCEEDED,
            provider_sequence=1,
            now=NOW,
        )
    )


def _candidate(
    execution: RuntimeExecution,
    *,
    event_id: str = "event-a",
    phase=RuntimePhase.SUCCEEDED,
):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=NOW,
        provider_event_id=event_id,
        output={} if phase is RuntimePhase.SUCCEEDED else None,
    )


class _Repo:
    def __init__(self, execution: RuntimeExecution):
        self.execution = execution
        self.observations: list[RuntimeObservationEvidence] = []
        self.anchors: list[RuntimeObservationEvidence] = []
        self.incidents: dict[tuple[str, str], RuntimeIntegrityIncident] = {}

    def get_execution(self, execution_id, *, tenant_id, for_update=False):
        if execution_id == self.execution.id and tenant_id == self.execution.tenant_id:
            return self.execution
        return None

    def accepted_terminal_observations(self, execution_id, *, tenant_id, phase):
        if execution_id != self.execution.id or tenant_id != self.execution.tenant_id:
            return []
        return [anchor for anchor in self.anchors if anchor.phase is phase]

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

    def add_integrity_incident_with_created(self, value):
        key = (value.accepted_observation_digest, value.conflicting_observation_digest)
        current = self.incidents.get(key)
        if current is not None:
            if current != value and (
                current.id != value.id
                or current.accepted_observation_id != value.accepted_observation_id
                or current.conflicting_observation_id != value.conflicting_observation_id
                or current.accepted_phase is not value.accepted_phase
                or current.conflicting_phase is not value.conflicting_phase
                or current.reason != value.reason
                or current.created_at != value.created_at
                or current.updated_at != value.updated_at
            ):
                raise RuntimeExecutionConflict("incident conflict")
            return current, False
        self.incidents[key] = value
        return value, True


class _Outbox:
    def __init__(self):
        self.messages = []

    def add(self, message):
        self.messages.append(message)


class _Uow:
    def __init__(self, repo):
        self.runtimes = repo
        self.outbox = _Outbox()


def _service(repo):
    return RuntimeRegistryService(
        uow_factory=lambda: None,
        tenant_id="tenant-a",
        feature_gates=FeatureGateSet.from_config("full", "managed_agent_runtime=true"),
    )


def _anchor(execution, candidate):
    return RuntimeObservationEvidence(
        id=uuid4(),
        tenant_id=execution.tenant_id,
        runtime_execution_id=execution.id,
        observation_id=candidate.observation_id,
        observation_digest=canonical_digest(candidate.to_dict()),
        assignment_id=execution.assignment_id,
        assignment_digest=execution.assignment_digest,
        provider_sequence=candidate.provider_sequence,
        phase=RuntimeExecutionPhase.SUCCEEDED,
        observed_at=NOW,
        received_at=NOW,
        safe_summary="accepted",
        processing_outcome=RuntimeObservationOutcome.APPLIED,
        provider_event_present=True,
    )


def test_late_terminal_opens_safe_incident_and_replays_without_side_effects():
    execution = _terminal_execution()
    repo = _Repo(execution)
    anchor_candidate = _candidate(execution)
    repo.anchors.append(_anchor(execution, anchor_candidate))
    uow = _Uow(repo)
    service = _service(repo)

    conflicting = _candidate(execution, event_id="event-b")
    first = service.record_late_terminal_observation_in_uow(
        uow,
        execution_id=execution.id,
        observation=conflicting,
        received_at=NOW,
    )
    assert first.kind is LateTerminalObservationResultKind.INCIDENT_OPENED
    assert len(repo.observations) == 1
    assert len(repo.incidents) == 1
    assert len(uow.outbox.messages) == 1
    assert "event-b" not in str(uow.outbox.messages[0].payload)
    assert "output" not in uow.outbox.messages[0].payload

    replay = service.record_late_terminal_observation_in_uow(
        uow,
        execution_id=execution.id,
        observation=conflicting,
        received_at=NOW,
    )
    assert replay.kind is LateTerminalObservationResultKind.INCIDENT_REPLAY
    assert len(repo.observations) == 1
    assert len(repo.incidents) == 1
    assert len(uow.outbox.messages) == 1

    second = service.record_late_terminal_observation_in_uow(
        uow,
        execution_id=execution.id,
        observation=_candidate(execution, event_id="event-c"),
        received_at=NOW,
    )
    assert second.kind is LateTerminalObservationResultKind.INCIDENT_OPENED
    assert len(repo.observations) == 2
    assert len(repo.incidents) == 2
    assert len(uow.outbox.messages) == 2
    assert execution.phase is RuntimeExecutionPhase.SUCCEEDED


def test_late_terminal_same_digest_is_accepted_replay():
    execution = _terminal_execution()
    repo = _Repo(execution)
    candidate = _candidate(execution)
    repo.anchors.append(_anchor(execution, candidate))
    uow = _Uow(repo)
    result = _service(repo).record_late_terminal_observation_in_uow(
        uow, execution_id=execution.id, observation=candidate, received_at=NOW
    )
    assert result.kind is LateTerminalObservationResultKind.ACCEPTED_REPLAY
    assert not repo.observations and not repo.incidents and not uow.outbox.messages


def test_late_terminal_requires_exactly_one_anchor():
    execution = _terminal_execution()
    repo = _Repo(execution)
    candidate = _candidate(execution)
    service = _service(repo)
    with pytest.raises(RuntimeExecutionConflict):
        service.record_late_terminal_observation_in_uow(
            _Uow(repo), execution_id=execution.id, observation=candidate, received_at=NOW
        )
    repo.anchors.extend([_anchor(execution, candidate), _anchor(execution, _candidate(execution))])
    with pytest.raises(RuntimeExecutionConflict):
        service.record_late_terminal_observation_in_uow(
            _Uow(repo), execution_id=execution.id, observation=candidate, received_at=NOW
        )


def test_late_terminal_rejects_wrong_identity_and_unknown_phase():
    execution = _terminal_execution()
    repo = _Repo(execution)
    repo.anchors.append(_anchor(execution, _candidate(execution)))
    service = _service(repo)
    wrong = _candidate(execution)
    object.__setattr__(wrong, "assignment_id", str(uuid4()))
    with pytest.raises(InvalidTaskInput):
        service.record_late_terminal_observation_in_uow(
            _Uow(repo), execution_id=execution.id, observation=wrong, received_at=NOW
        )
    lost = _candidate(execution, phase=RuntimePhase.LOST)
    object.__setattr__(lost, "output", None)
    with pytest.raises(InvalidTaskInput):
        service.record_late_terminal_observation_in_uow(
            _Uow(repo), execution_id=execution.id, observation=lost, received_at=NOW
        )


def test_late_terminal_rejects_non_terminal_runtime_execution():
    execution = _terminal_execution()
    repo = _Repo(execution)
    object.__setattr__(
        repo,
        "execution",
        RuntimeExecution.prepare(
            tenant_id=execution.tenant_id,
            run_id=execution.run_id,
            runtime_version_id=execution.runtime_version_id,
            assignment_id=execution.assignment_id,
            assignment_digest=execution.assignment_digest,
            dispatch_key=execution.dispatch_key,
            dispatch_digest=execution.dispatch_digest,
            now=NOW,
        ),
    )
    with pytest.raises(InvalidTaskTransition):
        _service(repo).record_late_terminal_observation_in_uow(
            _Uow(repo),
            execution_id=repo.execution.id,
            observation=_candidate(execution),
            received_at=NOW,
        )
