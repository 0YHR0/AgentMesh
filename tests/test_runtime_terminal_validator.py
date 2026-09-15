from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

import pytest

from agentmesh.application.coordinated_runtime_delivery import CoordinatedDispatchReceiptV1
from agentmesh.application.runtime_contracts import (
    TerminalObservationValidator,
    validate_terminal_observation,
)
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.runtime_sdk import RuntimeExecutionHandle, RuntimeObservation, RuntimePhase


def _observation(execution_id, assignment_id, *, phase=RuntimePhase.SUCCEEDED, **kwargs):
    output = kwargs.pop("output", {"ok": True} if phase is RuntimePhase.SUCCEEDED else None)
    return RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution_id),
        assignment_id=str(assignment_id),
        assignment_digest="a" * 64,
        phase=phase,
        observed_at=datetime.now(timezone.utc),
        provider_event_id="terminal-test",
        output=output,
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
        assert (
            validate_terminal_observation(
                observation,
                runtime_execution_id=execution_id,
                assignment_id=assignment_id,
                assignment_digest="a" * 64,
            )
            is observation
        )


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


def test_digest_thaws_nested_frozen_json_and_is_stable() -> None:
    execution_id, assignment_id = uuid4(), uuid4()
    observation = _observation(
        execution_id,
        assignment_id,
        progress={"steps": [{"name": "one", "meta": {"ok": True}}]},
        extensions={"provider": {"labels": ["a", "b"]}},
    )
    object.__setattr__(
        observation,
        "progress",
        MappingProxyType(
            {"steps": (MappingProxyType({"name": "one", "meta": MappingProxyType({"ok": True})}),)}
        ),
    )
    object.__setattr__(
        observation,
        "output",
        MappingProxyType({"result": MappingProxyType({"value": 1})}),
    )
    first = TerminalObservationValidator.digest(observation)
    second = TerminalObservationValidator.digest(observation)
    assert first == second
    assert type(observation.progress) is MappingProxyType
    assert type(observation.output) is MappingProxyType


def test_digest_changes_when_nested_observation_value_changes() -> None:
    execution_id, assignment_id = uuid4(), uuid4()
    original = _observation(
        execution_id,
        assignment_id,
        progress={"steps": [{"name": "one"}]},
    )
    changed = _observation(
        execution_id,
        assignment_id,
        progress={"steps": [{"name": "two"}]},
    )
    assert TerminalObservationValidator.digest(original) != TerminalObservationValidator.digest(
        changed
    )


def test_invalid_terminal_observation_is_rejected_before_digest_use() -> None:
    execution_id, assignment_id = uuid4(), uuid4()
    invalid = _observation(
        execution_id,
        assignment_id,
        phase=RuntimePhase.RUNNING,
    )
    with pytest.raises(InvalidTaskInput, match="terminal"):
        validate_terminal_observation(
            invalid,
            runtime_execution_id=execution_id,
            assignment_id=assignment_id,
            assignment_digest="a" * 64,
        )


def test_validator_accepts_frozen_output_from_normalized_receipt() -> None:
    execution_id, assignment_id = uuid4(), uuid4()
    source = _observation(execution_id, assignment_id, output={"nested": {"ok": True}})
    handle = RuntimeExecutionHandle(
        runtime_execution_id=str(execution_id),
        runtime_version_id=str(uuid4()),
        provider_execution_ref="provider",
        assignment_id=str(assignment_id),
        assignment_digest="a" * 64,
        created_at=source.observed_at,
    )
    receipt = CoordinatedDispatchReceiptV1(
        schema_version=1,
        dispatch_digest="b" * 64,
        assignment_digest="a" * 64,
        runtime_execution_id=execution_id,
        assignment_id=assignment_id,
        handle=handle,
        observation=source,
    )
    normalized = receipt.observation
    assert normalized is not None
    assert isinstance(normalized.output, MappingProxyType)
    assert (
        validate_terminal_observation(
            normalized,
            runtime_execution_id=execution_id,
            assignment_id=assignment_id,
            assignment_digest="a" * 64,
        )
        is normalized
    )
    assert TerminalObservationValidator.digest(normalized) == TerminalObservationValidator.digest(
        source
    )


@pytest.mark.parametrize("output", [["not", "a", "mapping"], "scalar", 42])
def test_validator_rejects_non_mapping_success_output(output) -> None:
    execution_id, assignment_id = uuid4(), uuid4()
    invalid = _observation(execution_id, assignment_id, output=output)
    with pytest.raises(InvalidTaskInput, match="mapping output"):
        validate_terminal_observation(
            invalid,
            runtime_execution_id=execution_id,
            assignment_id=assignment_id,
            assignment_digest="a" * 64,
        )
