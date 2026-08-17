from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from .assignment import RuntimeExecutionHandle
from .canonical import canonical_json_bytes
from .common import (
    API_VERSION,
    RuntimeContractError,
    RuntimePhase,
    _closed,
    _digest,
    _exact_bool,
    _exact_int,
    _exact_tuple,
    _expect_mapping,
    _schema,
    _text,
    _uuid,
)
from .observation import RuntimeErrorDTO, RuntimeObservation


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    errors: tuple[RuntimeErrorDTO, ...] = ()
    warnings: tuple[str, ...] = ()
    schema_name: ClassVar[str] = "agentmesh.runtime-validation-report"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        _exact_bool(self.valid, "valid")
        _exact_tuple(self.errors, "errors")
        _exact_tuple(self.warnings, "warnings")
        if any(type(error) is not RuntimeErrorDTO for error in self.errors):
            raise RuntimeContractError("validation errors must be RuntimeErrorDTO values")
        if self.valid and self.errors:
            raise RuntimeContractError("valid validation report cannot contain errors")
        for warning in self.warnings:
            _text(warning, "validation warning", max_bytes=1024)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "valid": self.valid,
            "errors": [error.to_dict() for error in self.errors],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ValidationReport:
        data = _expect_mapping(value, "validation_report")
        _closed(
            data,
            {"schema_name", "schema_version", "version", "valid", "errors", "warnings"},
            "validation_report",
        )
        _schema(data, cls.schema_name)
        if type(data.get("errors", [])) is not list or type(data.get("warnings", [])) is not list:
            raise RuntimeContractError("validation report errors/warnings must be arrays")
        return cls(
            valid=data.get("valid"),
            errors=tuple(RuntimeErrorDTO.from_dict(item) for item in data.get("errors", [])),
            warnings=tuple(data.get("warnings", [])),
        )


@dataclass(frozen=True)
class DispatchReceipt:
    dispatch_key: str
    runtime_execution_id: str
    assignment_digest: str
    handle: RuntimeExecutionHandle | None
    observation: RuntimeObservation
    schema_name: ClassVar[str] = "agentmesh.runtime-dispatch-receipt"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        _text(self.dispatch_key, "dispatch_key", max_bytes=512)
        _uuid(self.runtime_execution_id, "runtime_execution_id")
        _digest(self.assignment_digest, "assignment_digest")
        if type(self.handle) not in {RuntimeExecutionHandle, type(None)}:
            raise RuntimeContractError("handle must be RuntimeExecutionHandle")
        if type(self.observation) is not RuntimeObservation:
            raise RuntimeContractError("observation must be RuntimeObservation")
        if self.observation.assignment_digest != self.assignment_digest:
            raise RuntimeContractError("dispatch receipt assignment digest mismatch")
        if self.observation.runtime_execution_id != self.runtime_execution_id:
            raise RuntimeContractError("dispatch receipt execution identity mismatch")
        if self.handle is not None and self.handle.assignment_digest != self.assignment_digest:
            raise RuntimeContractError("dispatch handle assignment digest mismatch")
        if (
            self.handle is not None
            and self.handle.runtime_execution_id != self.runtime_execution_id
        ):
            raise RuntimeContractError("dispatch handle execution identity mismatch")
        if self.handle is not None and self.handle.assignment_id != self.observation.assignment_id:
            raise RuntimeContractError("dispatch handle assignment identity mismatch")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "dispatch_key": self.dispatch_key,
            "runtime_execution_id": self.runtime_execution_id,
            "assignment_digest": self.assignment_digest,
            "observation": self.observation.to_dict(),
        }
        if self.handle is not None:
            result["handle"] = self.handle.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DispatchReceipt:
        data = _expect_mapping(value, "dispatch_receipt")
        _closed(
            data,
            {
                "schema_name",
                "schema_version",
                "version",
                "dispatch_key",
                "runtime_execution_id",
                "assignment_digest",
                "handle",
                "observation",
            },
            "dispatch_receipt",
        )
        _schema(data, cls.schema_name)
        return cls(
            dispatch_key=data.get("dispatch_key"),
            runtime_execution_id=_uuid(data.get("runtime_execution_id"), "runtime_execution_id"),
            assignment_digest=_digest(data.get("assignment_digest"), "assignment_digest") or "",
            handle=RuntimeExecutionHandle.from_dict(data["handle"])
            if data.get("handle") is not None
            else None,
            observation=RuntimeObservation.from_dict(data.get("observation")),
        )


@dataclass(frozen=True)
class LifecycleReceipt:
    operation_id: str
    runtime_execution_id: str
    operation: str
    accepted: bool
    observed_phase: RuntimePhase | None = None
    safe_message: str = ""
    schema_name: ClassVar[str] = "agentmesh.runtime-lifecycle-receipt"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        _text(self.operation_id, "operation_id", max_bytes=512)
        _uuid(self.runtime_execution_id, "runtime_execution_id")
        _exact_bool(self.accepted, "accepted")
        if self.observed_phase is not None and type(self.observed_phase) is not RuntimePhase:
            raise RuntimeContractError("observed_phase must be RuntimePhase")
        if self.operation not in {"cancel", "pause", "resume"}:
            raise RuntimeContractError("unknown lifecycle operation")
        if self.safe_message:
            _text(self.safe_message, "safe_message", max_bytes=2048)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "runtime_execution_id": self.runtime_execution_id,
            "operation": self.operation,
            "accepted": self.accepted,
            "safe_message": self.safe_message,
        }
        if self.observed_phase is not None:
            result["observed_phase"] = self.observed_phase
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LifecycleReceipt:
        data = _expect_mapping(value, "lifecycle_receipt")
        _closed(
            data,
            {
                "schema_name",
                "schema_version",
                "version",
                "operation_id",
                "runtime_execution_id",
                "operation",
                "accepted",
                "observed_phase",
                "safe_message",
            },
            "lifecycle_receipt",
        )
        _schema(data, cls.schema_name)
        phase = data.get("observed_phase")
        return cls(
            operation_id=data.get("operation_id"),
            runtime_execution_id=_uuid(data.get("runtime_execution_id"), "runtime_execution_id"),
            operation=data.get("operation"),
            accepted=data.get("accepted"),
            observed_phase=RuntimePhase(phase) if phase is not None else None,
            safe_message=data.get("safe_message", ""),
        )


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    runtime_execution_id: str
    assignment_digest: str
    sequence: int
    observation: RuntimeObservation

    def __post_init__(self) -> None:
        _text(self.event_id, "event_id", max_bytes=512)
        _uuid(self.runtime_execution_id, "runtime_execution_id")
        _digest(self.assignment_digest, "assignment_digest")
        if type(self.observation) is not RuntimeObservation:
            raise RuntimeContractError("observation must be RuntimeObservation")
        _exact_int(self.sequence, "sequence", minimum=0)
        if self.observation.assignment_digest != self.assignment_digest:
            raise RuntimeContractError("event assignment digest mismatch")
        if self.observation.runtime_execution_id != self.runtime_execution_id:
            raise RuntimeContractError("event execution identity mismatch")
        if len(canonical_json_bytes(self.to_dict())) > 65_536:
            raise RuntimeContractError("event exceeds the event size limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "runtime_execution_id": self.runtime_execution_id,
            "assignment_digest": self.assignment_digest,
            "sequence": self.sequence,
            "observation": self.observation.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeEvent:
        data = _expect_mapping(value, "runtime_event")
        _closed(
            data,
            {"event_id", "runtime_execution_id", "assignment_digest", "sequence", "observation"},
            "runtime_event",
        )
        return cls(
            event_id=data.get("event_id"),
            runtime_execution_id=_uuid(data.get("runtime_execution_id"), "runtime_execution_id"),
            assignment_digest=_digest(data.get("assignment_digest"), "assignment_digest") or "",
            sequence=data.get("sequence"),
            observation=RuntimeObservation.from_dict(data.get("observation")),
        )


@dataclass(frozen=True)
class RuntimeEventPage:
    events: tuple[RuntimeEvent, ...]
    next_cursor: str | None = None
    has_more: bool = False

    def __post_init__(self) -> None:
        _exact_tuple(self.events, "events")
        _exact_bool(self.has_more, "has_more")
        if any(type(event) is not RuntimeEvent for event in self.events):
            raise RuntimeContractError("events must contain RuntimeEvent values")
        if len(self.events) > 256:
            raise RuntimeContractError("event page exceeds its limit")
        if self.next_cursor is not None:
            _text(self.next_cursor, "next_cursor", max_bytes=1024)

    def to_dict(self) -> dict[str, Any]:
        result = {"events": [event.to_dict() for event in self.events], "has_more": self.has_more}
        if self.next_cursor is not None:
            result["next_cursor"] = self.next_cursor
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeEventPage:
        data = _expect_mapping(value, "runtime_event_page")
        _closed(data, {"events", "next_cursor", "has_more"}, "runtime_event_page")
        if type(data.get("events", [])) is not list:
            raise RuntimeContractError("events must be an array")
        return cls(
            events=tuple(RuntimeEvent.from_dict(item) for item in data.get("events", [])),
            next_cursor=data.get("next_cursor"),
            has_more=data.get("has_more", False),
        )


# The specification calls this DTO RuntimeError.  Keep the descriptive name
# above internally so importing it cannot be confused with Python exceptions.
RuntimeError = RuntimeErrorDTO
