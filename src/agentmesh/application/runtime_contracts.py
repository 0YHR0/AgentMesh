"""Application-owned Runtime result contracts.

The Runtime SDK validates the wire shape of an observation.  This module owns
the stricter control-plane terminal contract: it binds an observation to the
dispatch context and rejects evidence that cannot safely be applied to Task,
Run, or Attempt state.  Keeping this validator outside the SDK also prevents
provider/framework concerns from leaking into the domain layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase, canonical_digest

_TERMINAL = frozenset(
    {
        RuntimePhase.SUCCEEDED,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELED,
        RuntimePhase.TIMED_OUT,
        RuntimePhase.LOST,
        RuntimePhase.OUTCOME_UNKNOWN,
    }
)
_KNOWN_TERMINAL = frozenset(
    {
        RuntimePhase.SUCCEEDED,
        RuntimePhase.FAILED,
        RuntimePhase.CANCELED,
        RuntimePhase.TIMED_OUT,
    }
)


@dataclass(frozen=True)
class TerminalObservationContext:
    """Immutable identity and policy facts captured at dispatch time."""

    runtime_execution_id: UUID
    assignment_id: UUID
    assignment_digest: str


class TerminalObservationValidator:
    """Validate the A4.2 terminal observation contract in one place."""

    @staticmethod
    def validate(
        observation: RuntimeObservation,
        context: TerminalObservationContext,
        *,
        require_known_terminal: bool = False,
    ) -> RuntimeObservation:
        if type(observation) is not RuntimeObservation:
            raise InvalidTaskInput("Runtime terminal observation has an invalid type")
        if type(context) is not TerminalObservationContext:
            raise InvalidTaskInput("Runtime terminal observation context is invalid")
        expected_execution = str(context.runtime_execution_id)
        expected_assignment = str(context.assignment_id)
        if (
            observation.runtime_execution_id != expected_execution
            or observation.assignment_id != expected_assignment
            or observation.assignment_digest != context.assignment_digest
        ):
            raise InvalidTaskInput(
                "Runtime terminal observation execution identity does not match"
            )
        if observation.phase not in _TERMINAL:
            if require_known_terminal:
                raise InvalidTaskInput(
                    "Runtime terminal observation requires a known terminal phase"
                )
            raise InvalidTaskInput("Runtime terminal observation must be terminal")
        if require_known_terminal and observation.phase not in _KNOWN_TERMINAL:
            raise InvalidTaskInput("Runtime terminal observation requires a known terminal phase")
        if observation.usage:
            raise InvalidTaskInput("Terminal Runtime observations require empty usage")
        if observation.governed_action_requests or observation.wait_refs:
            raise InvalidTaskInput(
                "Terminal Runtime observations cannot retain action or wait requests"
            )
        if observation.phase is RuntimePhase.SUCCEEDED:
            if type(observation.output) is not dict:
                raise InvalidTaskInput("Runtime success requires mapping output")
            if observation.error is not None:
                raise InvalidTaskInput("Runtime success cannot carry an error")
        elif observation.output is not None or observation.output_artifact_refs:
            raise InvalidTaskInput("Non-success Runtime observations cannot carry output")
        return observation

    @staticmethod
    def digest(observation: RuntimeObservation) -> str:
        """Return the canonical evidence digest used by repositories."""

        return canonical_digest(observation.to_dict())


def validate_terminal_observation(
    observation: RuntimeObservation,
    *,
    runtime_execution_id: UUID,
    assignment_id: UUID,
    assignment_digest: str,
    require_known_terminal: bool = False,
) -> RuntimeObservation:
    """Small functional entry point for adapters and reconciliation callers."""

    return TerminalObservationValidator.validate(
        observation,
        TerminalObservationContext(
            runtime_execution_id=runtime_execution_id,
            assignment_id=assignment_id,
            assignment_digest=assignment_digest,
        ),
        require_known_terminal=require_known_terminal,
    )


__all__ = [
    "TerminalObservationContext",
    "TerminalObservationValidator",
    "validate_terminal_observation",
]
