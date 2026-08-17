from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from .canonical import canonical_json_bytes, normalize_utc
from .common import (
    API_VERSION,
    ErrorCategory,
    RetryDisposition,
    RuntimeContractError,
    RuntimePhase,
    _bounded,
    _closed,
    _digest,
    _exact_dict,
    _exact_int,
    _exact_tuple,
    _expect_mapping,
    _id_list,
    _reject_authority,
    _reject_secrets,
    _schema,
    _text,
    _timestamp,
    _uuid,
)
from .descriptor import ArtifactRef, _artifact_refs


@dataclass(frozen=True)
class RuntimeErrorDTO:
    code: str
    category: ErrorCategory
    message: str
    retry_disposition: RetryDisposition
    retry_after: datetime | None = None
    provider_code_digest: str | None = None
    evidence_refs: tuple[str, ...] = ()
    schema_name: ClassVar[str] = "agentmesh.runtime-error"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        _text(self.code, "error.code", max_bytes=128)
        _text(self.message, "error.message", max_bytes=4096)
        if type(self.category) is not ErrorCategory:
            raise RuntimeContractError("error.category must be ErrorCategory")
        if type(self.retry_disposition) is not RetryDisposition:
            raise RuntimeContractError("error.retry_disposition must be RetryDisposition")
        _exact_tuple(self.evidence_refs, "evidence_refs")
        if self.retry_after is not None:
            normalize_utc(self.retry_after)
        if self.provider_code_digest is not None:
            _digest(self.provider_code_digest, "provider_code_digest", required=False)
        if len(self.evidence_refs) > 128:
            raise RuntimeContractError("evidence_refs exceeds its count limit")
        for ref in self.evidence_refs:
            _text(ref, "evidence_refs item", max_bytes=1024)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "retry_disposition": self.retry_disposition,
            "evidence_refs": list(self.evidence_refs),
        }
        for name, value in (
            ("retry_after", self.retry_after),
            ("provider_code_digest", self.provider_code_digest),
        ):
            if value is not None:
                result[name] = value
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeErrorDTO:
        data = _expect_mapping(value, "error")
        _schema(data, cls.schema_name)
        _closed(
            data,
            {
                "schema_name",
                "schema_version",
                "code",
                "category",
                "message",
                "retry_disposition",
                "retry_after",
                "provider_code_digest",
                "evidence_refs",
            },
            "error",
        )
        try:
            category = ErrorCategory(data.get("category"))
            disposition = RetryDisposition(data.get("retry_disposition"))
        except ValueError as exc:
            raise RuntimeContractError("unsupported error category or retry disposition") from exc
        return cls(
            code=_text(data.get("code"), "error.code", max_bytes=128),
            category=category,
            message=_text(data.get("message"), "error.message", max_bytes=4096),
            retry_disposition=disposition,
            retry_after=_timestamp(data.get("retry_after"), "retry_after", required=False),
            provider_code_digest=_digest(
                data.get("provider_code_digest"), "provider_code_digest", required=False
            ),
            evidence_refs=_id_list(data.get("evidence_refs"), "evidence_refs"),
        )


@dataclass(frozen=True)
class RuntimeObservation:
    observation_id: str
    runtime_execution_id: str
    assignment_id: str
    assignment_digest: str
    phase: RuntimePhase
    observed_at: datetime
    provider_event_id: str | None = None
    snapshot_digest: str | None = None
    provider_sequence: int | None = None
    progress: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_ref: str | None = None
    workspace_ref: str | None = None
    output: Any | None = None
    output_artifact_refs: tuple[ArtifactRef, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    governed_action_requests: tuple[Mapping[str, Any], ...] = ()
    wait_refs: tuple[str, ...] = ()
    error: RuntimeErrorDTO | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)
    schema_name: ClassVar[str] = "agentmesh.runtime-observation"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        for name in ("observation_id", "runtime_execution_id", "assignment_id"):
            _uuid(getattr(self, name), name)
        _digest(self.assignment_digest, "assignment_digest")
        if type(self.phase) is not RuntimePhase:
            raise RuntimeContractError("phase must be a RuntimePhase")
        normalize_utc(self.observed_at)
        if self.provider_event_id is None and self.snapshot_digest is None:
            raise RuntimeContractError("observation needs provider_event_id or snapshot_digest")
        for name in ("provider_event_id", "checkpoint_ref", "workspace_ref"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name, max_bytes=4096)
        if self.snapshot_digest is not None:
            _digest(self.snapshot_digest, "snapshot_digest", required=False)
        if self.provider_sequence is not None and (
            type(self.provider_sequence) is not int or self.provider_sequence < 0
        ):
            raise RuntimeContractError("provider_sequence must be a non-negative integer")
        if self.provider_sequence is not None:
            _exact_int(self.provider_sequence, "provider_sequence", minimum=0)
        for name in ("progress", "usage", "extensions"):
            _exact_dict(getattr(self, name), name)
        _exact_tuple(self.output_artifact_refs, "output_artifact_refs")
        _exact_tuple(self.governed_action_requests, "governed_action_requests")
        _exact_tuple(self.wait_refs, "wait_refs")
        for ref in self.wait_refs:
            _text(ref, "wait_refs item", max_bytes=1024)
        if any(type(ref) is not ArtifactRef for ref in self.output_artifact_refs):
            raise RuntimeContractError("output_artifact_refs must contain ArtifactRef values")
        if any(type(item) is not dict for item in self.governed_action_requests):
            raise RuntimeContractError("governed_action_requests must contain objects")
        if self.error is not None and type(self.error) is not RuntimeErrorDTO:
            raise RuntimeContractError("error must be RuntimeErrorDTO")
        for name, value in (
            ("progress", self.progress),
            ("usage", self.usage),
            ("extensions", self.extensions),
            ("output", self.output),
        ):
            _bounded(value, path=name)
            _reject_secrets(value, path=name)
        _reject_authority(self.governed_action_requests, path="governed_action_requests")
        for request in self.governed_action_requests:
            _bounded(request, path="governed_action_requests")
            _reject_secrets(request, path="governed_action_requests")
        if self.phase is not RuntimePhase.SUCCEEDED and (
            self.output is not None or self.output_artifact_refs
        ):
            raise RuntimeContractError("output is allowed only on terminal success")
        if len(canonical_json_bytes(self.to_dict())) > 65_536:
            raise RuntimeContractError("observation exceeds the event size limit")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "runtime_execution_id": self.runtime_execution_id,
            "assignment_id": self.assignment_id,
            "assignment_digest": self.assignment_digest,
            "phase": self.phase,
            "observed_at": self.observed_at,
            "progress": dict(self.progress),
            "usage": dict(self.usage),
            "output_artifact_refs": [ref.to_dict() for ref in self.output_artifact_refs],
            "governed_action_requests": [dict(item) for item in self.governed_action_requests],
            "wait_refs": list(self.wait_refs),
        }
        optional = {
            "provider_event_id": self.provider_event_id,
            "snapshot_digest": self.snapshot_digest,
            "provider_sequence": self.provider_sequence,
            "checkpoint_ref": self.checkpoint_ref,
            "workspace_ref": self.workspace_ref,
            "output": self.output,
            "error": self.error.to_dict() if self.error else None,
        }
        result.update({name: value for name, value in optional.items() if value is not None})
        if self.extensions:
            result["extensions"] = dict(self.extensions)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeObservation:
        data = _expect_mapping(value, "observation")
        _schema(data, cls.schema_name)
        _closed(
            data,
            {
                "schema_name",
                "schema_version",
                "observation_id",
                "runtime_execution_id",
                "assignment_id",
                "assignment_digest",
                "provider_event_id",
                "snapshot_digest",
                "provider_sequence",
                "phase",
                "observed_at",
                "progress",
                "checkpoint_ref",
                "workspace_ref",
                "output",
                "output_artifact_refs",
                "usage",
                "governed_action_requests",
                "wait_refs",
                "error",
                "extensions",
            },
            "observation",
        )
        phase_value = data.get("phase")
        try:
            phase = RuntimePhase(phase_value)
        except ValueError as exc:
            raise RuntimeContractError("unsupported runtime phase") from exc
        requests = data.get("governed_action_requests", [])
        if type(requests) is not list or any(type(item) is not dict for item in requests):
            raise RuntimeContractError("governed action request array required")
        return cls(
            observation_id=_uuid(data.get("observation_id"), "observation_id"),
            runtime_execution_id=_uuid(data.get("runtime_execution_id"), "runtime_execution_id"),
            assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
            assignment_digest=_digest(data.get("assignment_digest"), "assignment_digest") or "",
            phase=phase,
            observed_at=_timestamp(data.get("observed_at"), "observed_at"),
            provider_event_id=data.get("provider_event_id"),
            snapshot_digest=_digest(data.get("snapshot_digest"), "snapshot_digest", required=False),
            provider_sequence=data.get("provider_sequence"),
            progress=_expect_mapping(data.get("progress", {}), "progress"),
            checkpoint_ref=data.get("checkpoint_ref"),
            workspace_ref=data.get("workspace_ref"),
            output=data.get("output"),
            output_artifact_refs=_artifact_refs(
                data.get("output_artifact_refs"), "output_artifact_refs"
            ),
            usage=_expect_mapping(data.get("usage", {}), "usage"),
            governed_action_requests=tuple(dict(item) for item in requests),
            wait_refs=_id_list(data.get("wait_refs"), "wait_refs"),
            error=RuntimeErrorDTO.from_dict(data["error"])
            if data.get("error") is not None
            else None,
            extensions=_expect_mapping(data.get("extensions", {}), "extensions"),
        )
