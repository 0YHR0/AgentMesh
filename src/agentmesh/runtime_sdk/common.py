"""Versioned DTOs and validation for Managed Agent Runtime API v0.1."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from .canonical import MAX_SAFE_INTEGER, canonical_json_bytes, normalize_utc

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
    if type(value) is not dict:
        raise RuntimeContractError(f"{name} must be an object")
    return dict(value)


def _exact_dict(value: Any, name: str) -> None:
    if type(value) is not dict:
        raise RuntimeContractError(f"{name} must be a JSON object")


def _exact_bool(value: Any, name: str) -> None:
    if type(value) is not bool:
        raise RuntimeContractError(f"{name} must be a boolean")


def _exact_int(value: Any, name: str, *, minimum: int | None = None) -> None:
    if type(value) is not int:
        raise RuntimeContractError(f"{name} must be an integer")
    if abs(value) > MAX_SAFE_INTEGER:
        raise RuntimeContractError(f"{name} exceeds the safe integer range")
    if minimum is not None and value < minimum:
        raise RuntimeContractError(f"{name} must be at least {minimum}")


def _closed(data: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise RuntimeContractError(f"{name} contains unknown fields: {sorted(unknown)}")


def _text(
    value: Any, name: str, *, required: bool = True, max_bytes: int = MAX_STRING_BYTES
) -> str:
    if type(value) is not str or (required and not value.strip()):
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
    if type(value) is datetime:
        result = value
    elif type(value) is str:
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
    if type(value) is str:
        if len(value.encode("utf-8", "surrogatepass")) > MAX_STRING_BYTES:
            raise RuntimeContractError(f"{path} exceeds maximum string size")
        try:
            canonical_json_bytes(value)
        except ValueError as exc:
            raise RuntimeContractError(f"{path} is not valid JSON text") from exc
        return
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise RuntimeContractError(f"{path} exceeds the safe integer range")
        return
    if type(value) is float:
        try:
            canonical_json_bytes(value)
        except ValueError as exc:
            raise RuntimeContractError(f"{path} is not a finite JSON number") from exc
        return
    if type(value) is dict:
        if len(value) > MAX_COLLECTION_ITEMS:
            raise RuntimeContractError(f"{path} has too many object members")
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeContractError(f"{path} contains a non-string key")
            _bounded(item, depth=depth + 1, path=f"{path}.{key}")
    elif type(value) is list or type(value) is tuple:
        if len(value) > MAX_COLLECTION_ITEMS:
            raise RuntimeContractError(f"{path} has too many items")
        for index, item in enumerate(value):
            _bounded(item, depth=depth + 1, path=f"{path}[{index}]")
    else:
        raise RuntimeContractError(f"{path} contains an unsupported JSON value")


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


def _reject_authority(value: Any, *, path: str = "payload") -> None:
    if type(value) is dict:
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered == "permit" or lowered.endswith("_permit") or lowered == "permit_id":
                raise RuntimeContractError(f"{path}.{key} contains Permit authority")
            _reject_authority(item, path=f"{path}.{key}")
    elif type(value) is list or type(value) is tuple:
        for index, item in enumerate(value):
            _reject_authority(item, path=f"{path}[{index}]")


def _schema(data: Mapping[str, Any], expected: str) -> None:
    if type(data.get("schema_name")) is not str or data.get("schema_name") not in (
        expected,
        expected.replace("-", "."),
    ):
        raise RuntimeContractError(f"schema_name must be {expected}")
    version = data.get("schema_version", data.get("version"))
    if type(version) is not int or version != API_VERSION:
        raise UnknownMajorVersion(f"unsupported {expected} major version: {version!r}")


def _id_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if type(value) is not list:
        raise RuntimeContractError(f"{name} must be an array")
    return tuple(_text(item, f"{name} item", max_bytes=512) for item in value)


def _exact_tuple(value: Any, name: str) -> None:
    if type(value) is not tuple:
        raise RuntimeContractError(f"{name} must be a tuple when constructed directly")


def _validate_required_capabilities(value: Mapping[str, Any]) -> None:
    for name, item in value.items():
        if name in {"reattach", "pause_resume", "checkpoint", "fork", "event_stream"}:
            _exact_bool(item, f"required_capabilities.{name}")
        elif name == "execution_mode":
            if type(item) is not list or any(type(entry) is not str for entry in item):
                raise RuntimeContractError(
                    "required_capabilities.execution_mode must be a string array"
                )
            if any(entry not in {"inline", "managed_async"} for entry in item):
                raise RuntimeContractError(
                    "required_capabilities.execution_mode has an unsupported value"
                )
        elif name in {"tool_bridge", "artifact_io", "isolation_profiles", "modalities"}:
            if type(item) is not list or any(type(entry) is not str for entry in item):
                raise RuntimeContractError(f"required_capabilities.{name} must be a string array")
        elif name == "cancel":
            if type(item) is not str or item not in {"none", "cooperative", "forced"}:
                raise RuntimeContractError("required_capabilities.cancel has an unsupported value")
