from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentmesh.application.runtime_contracts import validate_terminal_observation
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase


def _observation(execution_id, assignment_id, *, phase=RuntimePhase.SUCCEEDED, **kwargs):
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution_id),
        assignment_id=str(assignment_id),
        assignment_digest="a" * 64,
        phase=phase,
        observed_at=datetime.now(timezone.utc),
        provider_event_id="terminal-test",
        output={"ok": True} if phase is RuntimePhase.SUCCEEDED else None,
        **kwargs,
    )


def test_validator_accepts_known_and_control_plane_terminal_phases() -> None:
    execution_id, assignment_id = uuid4(), uuid4()
    for phase in (
        RuntimePhase.SUCCEEDED,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELED,
        RuntimePhase.TIMED_OUT,
        RuntimePhase.LOST,
        RuntimePhase.OUTCOME_UNKNOWN,
    ):
        observation = _observation(execution_id, assignment_id, phase=phase)
        assert validate_terminal_observation(
            observation,
            runtime_execution_id=execution_id,
            assignment_id=assignment_id,
            assignment_digest="a" * 64,
        ) is observation


def test_validator_rejects_identity_and_terminal_shape_conflicts() -> None:
    execution_id, assignment_id = uuid4(), uuid4()
    with pytest.raises(InvalidTaskInput, match="identity"):
        validate_terminal_observation(
            _observation(uuid4(), assignment_id),
            runtime_execution_id=execution_id,
            assignment_id=assignment_id,
            assignment_digest="a" * 64,
        )
    with pytest.raises(InvalidTaskInput, match="empty usage"):
        validate_terminal_observation(
            _observation(execution_id, assignment_id, usage={"tokens": 1}),
            runtime_execution_id=execution_id,
            assignment_id=assignment_id,
            assignment_digest="a" * 64,
        )


def test_validator_requires_known_terminal_for_reconciliation() -> None:
    execution_id, assignment_id = uuid4(), uuid4()
    with pytest.raises(InvalidTaskInput, match="known terminal"):
        validate_terminal_observation(
            _observation(execution_id, assignment_id, phase=RuntimePhase.LOST),
            runtime_execution_id=execution_id,
            assignment_id=assignment_id,
            assignment_digest="a" * 64,
            require_known_terminal=True,
        )
