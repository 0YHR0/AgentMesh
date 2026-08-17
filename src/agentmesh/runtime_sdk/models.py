"""Versioned DTOs and validation for Managed Agent Runtime API v0.1."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar
from uuid import UUID

from .canonical import canonical_digest, canonical_json_bytes, normalize_utc

API_VERSION = 1
RUNTIME_API_VERSION = API_VERSION
MAX_DEPTH = 32
MAX_COLLECTION_ITEMS = 256
MAX_STRING_BYTES = 262_144
_DIGEST = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_SECRET_KEYS = {
    "password",
    "passwd",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "secret_value",
    "credential_value",
}


class RuntimeContractError(ValueError):
    """Base error for malformed or unsafe Runtime contract data."""


class UnknownMajorVersion(RuntimeContractError):
    pass


class UnknownSecurityObligation(RuntimeContractError):
    pass


class UnknownCapability(RuntimeContractError):
    pass


class RuntimePhase(str, Enum):
    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"
    LOST = "LOST"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.CANCELED,
            self.TIMED_OUT,
            self.LOST,
            self.OUTCOME_UNKNOWN,
        }


class ErrorCategory(str, Enum):
    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    CONFLICT = "conflict"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    DEPENDENCY = "dependency"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


class RetryDisposition(str, Enum):
    NEVER = "NEVER"
    SAME_EXECUTION = "SAME_EXECUTION"
    NEW_EXECUTION = "NEW_EXECUTION"
    RECONCILE = "RECONCILE"
    OPERATOR = "OPERATOR"


KNOWN_CAPABILITIES = {
    "execution_mode",
    "reattach",
    "cancel",
    "pause_resume",
    "checkpoint",
    "fork",
    "event_stream",
    "tool_bridge",
    "artifact_io",
    "isolation_profiles",
    "modalities",
}
KNOWN_OBLIGATIONS = {
    "argument_caps",
    "approved_endpoint",
    "redaction",
    "evidence_retention",
    "network_profile",
    "approver_stages",
    "execution_window",
    "required_reconciliation_method",
    "recheck_at_execution",
}
TERMINAL_PHASES = {phase for phase in RuntimePhase if phase.terminal}


def _expect_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{name} must be an object")
    return dict(value)


def _text(
    value: Any, name: str, *, required: bool = True, max_bytes: int = MAX_STRING_BYTES
) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise RuntimeContractError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > max_bytes:
        raise RuntimeContractError(f"{name} exceeds its size limit")
    return value


def _uuid(value: Any, name: str) -> str:
    text = _text(value, name, max_bytes=64)
    try:
        return str(UUID(text))
    except ValueError as exc:
        raise RuntimeContractError(f"{name} must be a UUID") from exc


def _digest(value: Any, name: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    text = _text(value, name, max_bytes=71).lower()
    if not _DIGEST.fullmatch(text):
        raise RuntimeContractError(f"{name} must be a SHA-256 hex digest")
    return text.removeprefix("sha256:")


def _timestamp(value: Any, name: str, *, required: bool = True) -> datetime | None:
    if value is None and not required:
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeContractError(f"{name} must be RFC 3339") from exc
    else:
        raise RuntimeContractError(f"{name} must be RFC 3339")
    try:
        return normalize_utc(result)
    except ValueError as exc:
        raise RuntimeContractError(f"{name} must include a timezone") from exc


def _bounded(value: Any, *, depth: int = 0, path: str = "payload") -> None:
    if depth > MAX_DEPTH:
        raise RuntimeContractError(f"{path} exceeds maximum nesting depth")
    if isinstance(value, str) and len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise RuntimeContractError(f"{path} exceeds maximum string size")
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise RuntimeContractError(f"{path} has too many object members")
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeContractError(f"{path} contains a non-string key")
            _bounded(item, depth=depth + 1, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise RuntimeContractError(f"{path} has too many items")
        for index, item in enumerate(value):
            _bounded(item, depth=depth + 1, path=f"{path}[{index}]")


def _reject_secrets(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SECRET_KEYS or lowered.endswith("_secret_value"):
                raise RuntimeContractError(f"{path}.{key} contains a secret value")
            _reject_secrets(item, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_secrets(item, path=f"{path}[{index}]")


def _schema(data: Mapping[str, Any], expected: str) -> None:
    if data.get("schema_name") not in (expected, expected.replace("-", ".")):
        raise RuntimeContractError(f"schema_name must be {expected}")
    version = data.get("schema_version", data.get("version"))
    if version != API_VERSION:
        raise UnknownMajorVersion(f"unsupported {expected} major version: {version!r}")


def _id_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise RuntimeContractError(f"{name} must be an array")
    return tuple(_text(item, f"{name} item", max_bytes=512) for item in value)


@dataclass(frozen=True)
class Envelope:
    """Common cross-process envelope used by Runtime commands and events."""

    schema_name: str
    schema_version: int
    message_id: str
    tenant_id: str
    occurred_at: datetime
    producer: str
    actor: str
    correlation_id: str
    payload: Mapping[str, Any]
    idempotency_key: str | None = None
    causation_id: str | None = None
    trace_context: Mapping[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.schema_name, "schema_name", max_bytes=256)
        if self.schema_version != API_VERSION:
            raise UnknownMajorVersion(
                f"unsupported envelope major version: {self.schema_version!r}"
            )
        for name in ("message_id", "correlation_id"):
            _uuid(getattr(self, name), name)
        if self.causation_id is not None:
            _uuid(self.causation_id, "causation_id")
        _text(self.tenant_id, "tenant_id", max_bytes=256)
        _timestamp(self.occurred_at, "occurred_at")
        _text(self.producer, "producer", max_bytes=256)
        _text(self.actor, "actor", max_bytes=512)
        if self.idempotency_key is not None:
            _text(self.idempotency_key, "idempotency_key", max_bytes=512)
        if self.expires_at is not None:
            _timestamp(self.expires_at, "expires_at")
        for name, value in (
            ("payload", self.payload),
            ("trace_context", self.trace_context),
            ("extensions", self.extensions),
        ):
            _bounded(value, path=name)
            _reject_secrets(value, path=name)
        if len(canonical_json_bytes(self.to_dict())) > 256 * 1024:
            raise RuntimeContractError("envelope exceeds the 256 KiB limit")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "tenant_id": self.tenant_id,
            "occurred_at": self.occurred_at,
            "producer": self.producer,
            "actor": self.actor,
            "correlation_id": self.correlation_id,
            "payload": dict(self.payload),
        }
        optional = {
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "trace_context": dict(self.trace_context) if self.trace_context else None,
            "expires_at": self.expires_at,
            "extensions": dict(self.extensions) if self.extensions else None,
        }
        result.update({name: value for name, value in optional.items() if value is not None})
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Envelope:
        data = _expect_mapping(value, "envelope")
        return cls(
            schema_name=_text(data.get("schema_name"), "schema_name", max_bytes=256),
            schema_version=data.get("schema_version"),
            message_id=_uuid(data.get("message_id"), "message_id"),
            tenant_id=_text(data.get("tenant_id"), "tenant_id", max_bytes=256),
            occurred_at=_timestamp(data.get("occurred_at"), "occurred_at")
            or datetime.now().astimezone(),
            producer=_text(data.get("producer"), "producer", max_bytes=256),
            actor=_text(data.get("actor"), "actor", max_bytes=512),
            correlation_id=_uuid(data.get("correlation_id"), "correlation_id"),
            payload=_expect_mapping(data.get("payload"), "payload"),
            idempotency_key=data.get("idempotency_key"),
            causation_id=data.get("causation_id"),
            trace_context=_expect_mapping(data.get("trace_context", {}), "trace_context"),
            expires_at=_timestamp(data.get("expires_at"), "expires_at", required=False),
            extensions=_expect_mapping(data.get("extensions", {}), "extensions"),
        )


@dataclass(frozen=True)
class RuntimeLimits:
    max_assignment_bytes: int = 262_144
    max_event_bytes: int = 65_536
    max_result_bytes: int = 262_144
    max_artifact_refs: int = 128

    def __post_init__(self) -> None:
        for name in (
            "max_assignment_bytes",
            "max_event_bytes",
            "max_result_bytes",
            "max_artifact_refs",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise RuntimeContractError(f"{name} must be a positive integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_assignment_bytes": self.max_assignment_bytes,
            "max_event_bytes": self.max_event_bytes,
            "max_result_bytes": self.max_result_bytes,
            "max_artifact_refs": self.max_artifact_refs,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeLimits:
        data = _expect_mapping(value, "limits")
        return cls(
            **{name: data.get(name, getattr(cls(), name)) for name in cls.__dataclass_fields__}
        )


@dataclass(frozen=True)
class RuntimeCapabilities:
    execution_mode: tuple[str, ...] = ("inline",)
    reattach: bool = False
    cancel: str = "cooperative"
    pause_resume: bool = False
    checkpoint: bool = False
    fork: bool = False
    event_stream: bool = False
    tool_bridge: tuple[str, ...] = ()
    artifact_io: tuple[str, ...] = ("reference",)
    isolation_profiles: tuple[str, ...] = ("trusted-in-process",)
    modalities: tuple[str, ...] = ("text",)

    def __post_init__(self) -> None:
        if not self.execution_mode or any(
            item not in {"inline", "managed_async"} for item in self.execution_mode
        ):
            raise RuntimeContractError("capabilities.execution_mode contains an unsupported value")
        if self.cancel not in {"none", "cooperative", "forced"}:
            raise RuntimeContractError("capabilities.cancel contains an unsupported value")
        for name, values, allowed in (
            ("tool_bridge", self.tool_bridge, {"governed_action_v1"}),
            ("artifact_io", self.artifact_io, {"reference"}),
            (
                "isolation_profiles",
                self.isolation_profiles,
                {"trusted-in-process", "isolated", "remote"},
            ),
            ("modalities", self.modalities, {"text", "structured", "image", "audio"}),
        ):
            if any(item not in allowed for item in values):
                raise RuntimeContractError(f"capabilities.{name} contains an unsupported value")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_mode": list(self.execution_mode),
            "reattach": self.reattach,
            "cancel": self.cancel,
            "pause_resume": self.pause_resume,
            "checkpoint": self.checkpoint,
            "fork": self.fork,
            "event_stream": self.event_stream,
            "tool_bridge": list(self.tool_bridge),
            "artifact_io": list(self.artifact_io),
            "isolation_profiles": list(self.isolation_profiles),
            "modalities": list(self.modalities),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeCapabilities:
        data = _expect_mapping(value, "capabilities")
        unknown = set(data) - KNOWN_CAPABILITIES
        if unknown:
            raise UnknownCapability(f"unknown runtime capabilities: {sorted(unknown)}")
        converted: dict[str, Any] = {}
        for name in KNOWN_CAPABILITIES:
            if name in data:
                item = data[name]
                if name in {
                    "execution_mode",
                    "tool_bridge",
                    "artifact_io",
                    "isolation_profiles",
                    "modalities",
                }:
                    if not isinstance(item, list):
                        raise RuntimeContractError(f"capabilities.{name} must be an array")
                    converted[name] = tuple(
                        _text(entry, f"capabilities.{name} item", max_bytes=128) for entry in item
                    )
                else:
                    converted[name] = item
        return cls(**converted)


@dataclass(frozen=True)
class RuntimeDescriptor:
    runtime_key: str
    display_name: str
    adapter_kind: str
    capabilities: RuntimeCapabilities
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    extensions: Mapping[str, Any] = field(default_factory=dict)
    schema_name: ClassVar[str] = "agentmesh.runtime-descriptor"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        _text(self.runtime_key, "runtime_key", max_bytes=256)
        _text(self.display_name, "display_name", max_bytes=512)
        _text(self.adapter_kind, "adapter_kind", max_bytes=256)
        _bounded(self.extensions, path="extensions")
        _reject_secrets(self.extensions, path="extensions")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "runtime_key": self.runtime_key,
            "display_name": self.display_name,
            "adapter_kind": self.adapter_kind,
            "capabilities": self.capabilities.to_dict(),
            "limits": self.limits.to_dict(),
        }
        if self.extensions:
            result["extensions"] = dict(self.extensions)
        return result

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeDescriptor:
        data = _expect_mapping(value, "descriptor")
        _schema(data, cls.schema_name)
        return cls(
            runtime_key=_text(data.get("runtime_key"), "runtime_key", max_bytes=256),
            display_name=_text(data.get("display_name"), "display_name", max_bytes=512),
            adapter_kind=_text(data.get("adapter_kind"), "adapter_kind", max_bytes=256),
            capabilities=RuntimeCapabilities.from_dict(data.get("capabilities", {})),
            limits=RuntimeLimits.from_dict(data.get("limits", {})),
            extensions=_expect_mapping(data.get("extensions", {}), "extensions"),
        )


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    version_id: str
    digest: str
    size_bytes: int | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.artifact_id, "artifact_id")
        _uuid(self.version_id, "version_id")
        _digest(self.digest, "artifact digest")
        if self.size_bytes is not None and (
            not isinstance(self.size_bytes, int) or self.size_bytes < 0
        ):
            raise RuntimeContractError("artifact size_bytes must be non-negative")
        if self.media_type is not None:
            _text(self.media_type, "artifact media_type", max_bytes=256)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "version_id": self.version_id,
            "digest": self.digest,
        }
        if self.size_bytes is not None:
            result["size_bytes"] = self.size_bytes
        if self.media_type is not None:
            result["media_type"] = self.media_type
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactRef:
        data = _expect_mapping(value, "artifact_ref")
        return cls(
            artifact_id=_uuid(data.get("artifact_id"), "artifact_id"),
            version_id=_uuid(data.get("version_id"), "version_id"),
            digest=_digest(data.get("digest"), "artifact digest") or "",
            size_bytes=data.get("size_bytes"),
            media_type=data.get("media_type"),
        )


def _artifact_refs(value: Any, name: str = "artifact_refs") -> tuple[ArtifactRef, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeContractError(f"{name} must be an array")
    if len(value) > 128:
        raise RuntimeContractError(f"{name} exceeds its count limit")
    return tuple(ArtifactRef.from_dict(item) for item in value)


@dataclass(frozen=True)
class RuntimeAssignment:
    assignment_id: str
    tenant_id: str
    task_id: str
    run_id: str
    agent_definition_id: str
    agent_version_id: str
    agent_version_digest: str
    runtime_version_id: str
    runtime_descriptor_digest: str
    execution_mode: str
    run_role: str
    revision: int
    objective: str | None = None
    structured_input: Mapping[str, Any] | None = None
    input_artifact_refs: tuple[ArtifactRef, ...] = ()
    work_item_snapshot_version: int | None = None
    work_item_snapshot_digest: str | None = None
    acceptance_contract: Mapping[str, Any] = field(default_factory=dict)
    output_schema_digest: str | None = None
    required_capabilities: Mapping[str, Any] = field(default_factory=dict)
    tool_profile_version: str | None = None
    tool_snapshot_refs: tuple[str, ...] = ()
    capability_bundle_refs: tuple[str, ...] = ()
    policy_snapshot_ref: str | None = None
    required_obligations: tuple[str, ...] = ()
    principal_context_ref: str | None = None
    delegation_grant_ref: str | None = None
    budget_slice: Mapping[str, Any] = field(default_factory=dict)
    deadline: datetime | None = None
    per_operation_limits: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    allowed_artifact_operations: tuple[str, ...] = ()
    trace_context: Mapping[str, Any] = field(default_factory=dict)
    correlation_ids: Mapping[str, str] = field(default_factory=dict)
    assignment_digest: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)
    schema_name: ClassVar[str] = "agentmesh.runtime-assignment"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        for name in (
            "assignment_id",
            "task_id",
            "run_id",
            "agent_definition_id",
            "agent_version_id",
            "runtime_version_id",
        ):
            _uuid(getattr(self, name), name)
        _text(self.tenant_id, "tenant_id", max_bytes=256)
        for name in (
            "agent_version_digest",
            "runtime_descriptor_digest",
            "output_schema_digest",
            "work_item_snapshot_digest",
        ):
            if getattr(self, name) is not None:
                _digest(getattr(self, name), name, required=False)
        _text(self.execution_mode, "execution_mode", max_bytes=64)
        if self.execution_mode not in {"inline", "managed_async"}:
            raise RuntimeContractError("execution_mode contains an unsupported value")
        _text(self.run_role, "run_role", max_bytes=64)
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 0
        ):
            raise RuntimeContractError("revision must be a non-negative integer")
        if self.work_item_snapshot_version is not None and self.work_item_snapshot_version < 0:
            raise RuntimeContractError("work_item_snapshot_version must be non-negative")
        if (
            self.objective is None
            and not self.input_artifact_refs
            and self.structured_input is None
        ):
            raise RuntimeContractError(
                "assignment requires objective, structured input, or input artifacts"
            )
        if self.structured_input is not None:
            _bounded(self.structured_input, path="structured_input")
            _reject_secrets(self.structured_input, path="structured_input")
        for name, value in (
            ("acceptance_contract", self.acceptance_contract),
            ("required_capabilities", self.required_capabilities),
            ("budget_slice", self.budget_slice),
            ("per_operation_limits", self.per_operation_limits),
            ("trace_context", self.trace_context),
            ("correlation_ids", self.correlation_ids),
            ("extensions", self.extensions),
        ):
            _bounded(value, path=name)
            _reject_secrets(value, path=name)
        unknown_obligations = set(self.required_obligations) - KNOWN_OBLIGATIONS
        if unknown_obligations:
            raise UnknownSecurityObligation(
                f"unknown security obligations: {sorted(unknown_obligations)}"
            )
        unknown_capabilities = set(self.required_capabilities) - KNOWN_CAPABILITIES
        if unknown_capabilities:
            raise UnknownCapability(
                f"unknown required capabilities: {sorted(unknown_capabilities)}"
            )
        if self.deadline is not None:
            normalize_utc(self.deadline)
        if self.assignment_digest is None:
            object.__setattr__(self, "assignment_digest", self.digest())
        else:
            _digest(self.assignment_digest, "assignment_digest")
            if self.assignment_digest != self.digest():
                raise RuntimeContractError("assignment_digest does not match canonical assignment")

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "assignment_id": self.assignment_id,
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "agent_definition_id": self.agent_definition_id,
            "agent_version_id": self.agent_version_id,
            "agent_version_digest": self.agent_version_digest,
            "runtime_version_id": self.runtime_version_id,
            "runtime_descriptor_digest": self.runtime_descriptor_digest,
            "execution_mode": self.execution_mode,
            "run_role": self.run_role,
            "revision": self.revision,
            "acceptance_contract": dict(self.acceptance_contract),
            "required_capabilities": dict(self.required_capabilities),
            "tool_snapshot_refs": list(self.tool_snapshot_refs),
            "capability_bundle_refs": list(self.capability_bundle_refs),
            "required_obligations": list(self.required_obligations),
            "budget_slice": dict(self.budget_slice),
            "per_operation_limits": dict(self.per_operation_limits),
            "allowed_artifact_operations": list(self.allowed_artifact_operations),
            "trace_context": dict(self.trace_context),
            "correlation_ids": dict(self.correlation_ids),
            "input_artifact_refs": [item.to_dict() for item in self.input_artifact_refs],
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
        }
        optional = {
            "objective": self.objective,
            "structured_input": dict(self.structured_input)
            if self.structured_input is not None
            else None,
            "work_item_snapshot_version": self.work_item_snapshot_version,
            "work_item_snapshot_digest": self.work_item_snapshot_digest,
            "output_schema_digest": self.output_schema_digest,
            "tool_profile_version": self.tool_profile_version,
            "policy_snapshot_ref": self.policy_snapshot_ref,
            "principal_context_ref": self.principal_context_ref,
            "delegation_grant_ref": self.delegation_grant_ref,
            "deadline": self.deadline,
        }
        result.update({name: value for name, value in optional.items() if value is not None})
        if self.extensions:
            result["extensions"] = dict(self.extensions)
        if include_digest:
            result["assignment_digest"] = self.assignment_digest
        return result

    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeAssignment:
        data = _expect_mapping(value, "assignment")
        _schema(data, cls.schema_name)
        return cls(
            assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
            tenant_id=_text(data.get("tenant_id"), "tenant_id", max_bytes=256),
            task_id=_uuid(data.get("task_id"), "task_id"),
            run_id=_uuid(data.get("run_id"), "run_id"),
            agent_definition_id=_uuid(data.get("agent_definition_id"), "agent_definition_id"),
            agent_version_id=_uuid(data.get("agent_version_id"), "agent_version_id"),
            agent_version_digest=_digest(data.get("agent_version_digest"), "agent_version_digest")
            or "",
            runtime_version_id=_uuid(data.get("runtime_version_id"), "runtime_version_id"),
            runtime_descriptor_digest=_digest(
                data.get("runtime_descriptor_digest"), "runtime_descriptor_digest"
            )
            or "",
            execution_mode=_text(data.get("execution_mode"), "execution_mode", max_bytes=64),
            run_role=_text(data.get("run_role"), "run_role", max_bytes=64),
            revision=data.get("revision"),
            objective=data.get("objective"),
            structured_input=data.get("structured_input"),
            input_artifact_refs=_artifact_refs(data.get("input_artifact_refs")),
            work_item_snapshot_version=data.get("work_item_snapshot_version"),
            work_item_snapshot_digest=_digest(
                data.get("work_item_snapshot_digest"), "work_item_snapshot_digest", required=False
            ),
            acceptance_contract=_expect_mapping(
                data.get("acceptance_contract", {}), "acceptance_contract"
            ),
            output_schema_digest=_digest(
                data.get("output_schema_digest"), "output_schema_digest", required=False
            ),
            required_capabilities=_expect_mapping(
                data.get("required_capabilities", {}), "required_capabilities"
            ),
            tool_profile_version=data.get("tool_profile_version"),
            tool_snapshot_refs=_id_list(data.get("tool_snapshot_refs"), "tool_snapshot_refs"),
            capability_bundle_refs=_id_list(
                data.get("capability_bundle_refs"), "capability_bundle_refs"
            ),
            policy_snapshot_ref=data.get("policy_snapshot_ref"),
            required_obligations=tuple(
                _text(item, "required_obligations item", max_bytes=128)
                for item in data.get("required_obligations", [])
            ),
            principal_context_ref=data.get("principal_context_ref"),
            delegation_grant_ref=data.get("delegation_grant_ref"),
            budget_slice=_expect_mapping(data.get("budget_slice", {}), "budget_slice"),
            deadline=_timestamp(data.get("deadline"), "deadline", required=False),
            per_operation_limits=_expect_mapping(
                data.get("per_operation_limits", {}), "per_operation_limits"
            ),
            artifact_refs=_artifact_refs(data.get("artifact_refs")),
            allowed_artifact_operations=tuple(
                _text(item, "allowed_artifact_operations item", max_bytes=64)
                for item in data.get("allowed_artifact_operations", [])
            ),
            trace_context=_expect_mapping(data.get("trace_context", {}), "trace_context"),
            correlation_ids={
                str(k): _text(v, f"correlation_ids.{k}", max_bytes=512)
                for k, v in _expect_mapping(
                    data.get("correlation_ids", {}), "correlation_ids"
                ).items()
            },
            assignment_digest=_digest(
                data.get("assignment_digest"), "assignment_digest", required=False
            ),
            extensions=_expect_mapping(data.get("extensions", {}), "extensions"),
        )


@dataclass(frozen=True)
class RuntimeExecutionHandle:
    runtime_execution_id: str
    runtime_version_id: str
    provider_execution_ref: str
    assignment_id: str
    assignment_digest: str
    created_at: datetime
    provider_generation: str | None = None
    schema_name: ClassVar[str] = "agentmesh.runtime-execution-handle"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        for name in ("runtime_execution_id", "runtime_version_id", "assignment_id"):
            _uuid(getattr(self, name), name)
        _text(self.provider_execution_ref, "provider_execution_ref", max_bytes=4096)
        _reject_secrets({"provider_execution_ref": self.provider_execution_ref}, path="handle")
        _digest(self.assignment_digest, "assignment_digest")
        if self.provider_generation is not None:
            _text(self.provider_generation, "provider_generation", max_bytes=256)
        normalize_utc(self.created_at)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "runtime_execution_id": self.runtime_execution_id,
            "runtime_version_id": self.runtime_version_id,
            "provider_execution_ref": self.provider_execution_ref,
            "assignment_id": self.assignment_id,
            "assignment_digest": self.assignment_digest,
            "created_at": self.created_at,
        }
        if self.provider_generation is not None:
            result["provider_generation"] = self.provider_generation
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeExecutionHandle:
        data = _expect_mapping(value, "execution_handle")
        _schema(data, cls.schema_name)
        return cls(
            runtime_execution_id=_uuid(data.get("runtime_execution_id"), "runtime_execution_id"),
            runtime_version_id=_uuid(data.get("runtime_version_id"), "runtime_version_id"),
            provider_execution_ref=_text(
                data.get("provider_execution_ref"), "provider_execution_ref", max_bytes=4096
            ),
            assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
            assignment_digest=_digest(data.get("assignment_digest"), "assignment_digest") or "",
            created_at=_timestamp(data.get("created_at"), "created_at")
            or datetime.now().astimezone(),
            provider_generation=data.get("provider_generation"),
        )


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
        try:
            category = ErrorCategory(data.get("category"))
            disposition = RetryDisposition(data.get("retry_disposition"))
        except ValueError as exc:
            raise RuntimeContractError("unknown error category or retry disposition") from exc
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
        if not isinstance(self.phase, RuntimePhase):
            raise RuntimeContractError("phase must be a RuntimePhase")
        normalize_utc(self.observed_at)
        if self.provider_event_id is None and self.snapshot_digest is None:
            raise RuntimeContractError("observation needs provider_event_id or snapshot_digest")
        if self.snapshot_digest is not None:
            _digest(self.snapshot_digest, "snapshot_digest", required=False)
        if self.provider_sequence is not None and (
            not isinstance(self.provider_sequence, int) or self.provider_sequence < 0
        ):
            raise RuntimeContractError("provider_sequence must be a non-negative integer")
        for name, value in (
            ("progress", self.progress),
            ("usage", self.usage),
            ("extensions", self.extensions),
            ("output", self.output),
        ):
            _bounded(value, path=name)
            _reject_secrets(value, path=name)
        for request in self.governed_action_requests:
            _bounded(request, path="governed_action_requests")
            _reject_secrets(request, path="governed_action_requests")
            if "permit" in request or "permit_id" in request:
                raise RuntimeContractError("runtime observations cannot carry Permit authority")
        if self.phase is not RuntimePhase.SUCCEEDED and self.output is not None:
            raise RuntimeContractError("output is allowed only on terminal success")

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
        phase_value = data.get("phase")
        try:
            phase = RuntimePhase(phase_value)
        except ValueError as exc:
            raise RuntimeContractError(f"unknown runtime phase: {phase_value!r}") from exc
        requests = data.get("governed_action_requests", [])
        if not isinstance(requests, list) or any(
            not isinstance(item, Mapping) for item in requests
        ):
            raise RuntimeContractError("governed_action_requests must be an array of objects")
        return cls(
            observation_id=_uuid(data.get("observation_id"), "observation_id"),
            runtime_execution_id=_uuid(data.get("runtime_execution_id"), "runtime_execution_id"),
            assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
            assignment_digest=_digest(data.get("assignment_digest"), "assignment_digest") or "",
            phase=phase,
            observed_at=_timestamp(data.get("observed_at"), "observed_at")
            or datetime.now().astimezone(),
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


@dataclass(frozen=True)
class RuntimeResult:
    runtime_execution_id: str
    assignment_id: str
    assignment_digest: str
    runtime_version_id: str
    agent_version_id: str
    output: Any | None
    output_artifact_refs: tuple[ArtifactRef, ...]
    usage: Mapping[str, Any]
    usage_estimated: bool
    terminal_phase: RuntimePhase
    safe_summary: str
    produced_artifact_refs: tuple[ArtifactRef, ...] = ()
    governed_action_evidence_refs: tuple[str, ...] = ()
    result_digest: str | None = None
    schema_name: ClassVar[str] = "agentmesh.runtime-result"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
        for name in (
            "runtime_execution_id",
            "assignment_id",
            "runtime_version_id",
            "agent_version_id",
        ):
            _uuid(getattr(self, name), name)
        _digest(self.assignment_digest, "assignment_digest")
        if self.terminal_phase is not RuntimePhase.SUCCEEDED:
            raise RuntimeContractError("RuntimeResult represents terminal success only")
        if self.output is None and not self.output_artifact_refs:
            raise RuntimeContractError("successful result requires output or ArtifactRefs")
        _bounded(self.output, path="output")
        _reject_secrets(self.output, path="output")
        _bounded(self.usage, path="usage")
        _text(self.safe_summary, "safe_summary", max_bytes=4096)
        if self.result_digest is not None:
            _digest(self.result_digest, "result_digest", required=False)
            if self.result_digest != self.digest():
                raise RuntimeContractError("result_digest does not match canonical result")
        else:
            object.__setattr__(self, "result_digest", self.digest())

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "runtime_execution_id": self.runtime_execution_id,
            "assignment_id": self.assignment_id,
            "assignment_digest": self.assignment_digest,
            "runtime_version_id": self.runtime_version_id,
            "agent_version_id": self.agent_version_id,
            "output_artifact_refs": [ref.to_dict() for ref in self.output_artifact_refs],
            "usage": dict(self.usage),
            "usage_estimated": self.usage_estimated,
            "terminal_phase": self.terminal_phase,
            "safe_summary": self.safe_summary,
            "produced_artifact_refs": [ref.to_dict() for ref in self.produced_artifact_refs],
            "governed_action_evidence_refs": list(self.governed_action_evidence_refs),
        }
        if self.output is not None:
            result["output"] = self.output
        if include_digest:
            result["result_digest"] = self.result_digest
        return result

    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeResult:
        data = _expect_mapping(value, "result")
        _schema(data, cls.schema_name)
        try:
            phase = RuntimePhase(data.get("terminal_phase"))
        except ValueError as exc:
            raise RuntimeContractError("terminal_phase must be a known RuntimePhase") from exc
        return cls(
            runtime_execution_id=_uuid(data.get("runtime_execution_id"), "runtime_execution_id"),
            assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
            assignment_digest=_digest(data.get("assignment_digest"), "assignment_digest") or "",
            runtime_version_id=_uuid(data.get("runtime_version_id"), "runtime_version_id"),
            agent_version_id=_uuid(data.get("agent_version_id"), "agent_version_id"),
            output=data.get("output"),
            output_artifact_refs=_artifact_refs(
                data.get("output_artifact_refs"), "output_artifact_refs"
            ),
            usage=_expect_mapping(data.get("usage", {}), "usage"),
            usage_estimated=data.get("usage_estimated", False),
            terminal_phase=phase,
            safe_summary=_text(data.get("safe_summary"), "safe_summary", max_bytes=4096),
            produced_artifact_refs=_artifact_refs(
                data.get("produced_artifact_refs"), "produced_artifact_refs"
            ),
            governed_action_evidence_refs=_id_list(
                data.get("governed_action_evidence_refs"), "governed_action_evidence_refs"
            ),
            result_digest=_digest(data.get("result_digest"), "result_digest", required=False),
        )


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    errors: tuple[RuntimeErrorDTO, ...] = ()
    warnings: tuple[str, ...] = ()
    schema_name: ClassVar[str] = "agentmesh.runtime-validation-report"
    schema_version: ClassVar[int] = API_VERSION

    def __post_init__(self) -> None:
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
        if self.observation.assignment_digest != self.assignment_digest:
            raise RuntimeContractError("dispatch receipt assignment digest mismatch")
        if self.handle is not None and self.handle.assignment_digest != self.assignment_digest:
            raise RuntimeContractError("dispatch handle assignment digest mismatch")

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
        if self.sequence < 0:
            raise RuntimeContractError("event sequence must be non-negative")
        if self.observation.assignment_digest != self.assignment_digest:
            raise RuntimeContractError("event assignment digest mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "runtime_execution_id": self.runtime_execution_id,
            "assignment_digest": self.assignment_digest,
            "sequence": self.sequence,
            "observation": self.observation.to_dict(),
        }


@dataclass(frozen=True)
class RuntimeEventPage:
    events: tuple[RuntimeEvent, ...]
    next_cursor: str | None = None
    has_more: bool = False

    def __post_init__(self) -> None:
        if len(self.events) > 256:
            raise RuntimeContractError("event page exceeds its limit")
        if self.next_cursor is not None:
            _text(self.next_cursor, "next_cursor", max_bytes=1024)

    def to_dict(self) -> dict[str, Any]:
        result = {"events": [event.to_dict() for event in self.events], "has_more": self.has_more}
        if self.next_cursor is not None:
            result["next_cursor"] = self.next_cursor
        return result


# The specification calls this DTO RuntimeError.  Keep the descriptive name
# above internally so importing it cannot be confused with Python exceptions.
RuntimeError = RuntimeErrorDTO
