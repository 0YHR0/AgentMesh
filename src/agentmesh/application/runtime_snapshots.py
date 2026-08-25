"""Bounded persistence projections for orchestrated runtime snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.runtime_sdk.assignment import RuntimeAssignment, RuntimeExecutionHandle
from agentmesh.runtime_sdk.canonical import (
    CanonicalizationError,
    canonical_digest,
    canonical_json_bytes,
    decode_json,
)
from agentmesh.runtime_sdk.common import RuntimeContractError

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RuntimeAssignmentSnapshot:
    """Bounded immutable canonical Assignment projection kept off public DTOs."""

    id: UUID
    tenant_id: str
    runtime_execution_id: UUID
    contract_name: str
    contract_major: int
    assignment_id: UUID
    assignment_digest: str
    canonical_payload: MappingProxyType
    created_at: datetime

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID
            for value in (self.id, self.runtime_execution_id, self.assignment_id)
        ):
            raise InvalidTaskInput("Runtime Assignment snapshot identity is invalid")
        if (
            type(self.tenant_id) is not str
            or not self.tenant_id.strip()
            or len(self.tenant_id) > 128
            or type(self.contract_name) is not str
            or not self.contract_name.strip()
            or len(self.contract_name) > 128
            or type(self.contract_major) is not int
            or self.contract_major < 1
            or type(self.assignment_digest) is not str
            or _DIGEST.fullmatch(self.assignment_digest) is None
            or type(self.created_at) is not datetime
            or self.created_at.tzinfo is None
        ):
            raise InvalidTaskInput("Runtime Assignment snapshot is invalid")
        payload = snapshot_payload(self.canonical_payload, limit=262_144)
        assignment = parse_assignment_payload(payload)
        if (
            self.contract_name != assignment.schema_name
            or self.contract_major != assignment.schema_version
            or self.tenant_id != assignment.tenant_id
            or str(self.assignment_id) != assignment.assignment_id
            or self.assignment_digest != assignment.assignment_digest
        ):
            raise InvalidTaskInput("Runtime Assignment snapshot identity does not match payload")
        object.__setattr__(self, "canonical_payload", payload)


@dataclass(frozen=True)
class RuntimeHandleSnapshot:
    """Bounded immutable canonical execution handle projection."""

    id: UUID
    tenant_id: str
    runtime_execution_id: UUID
    handle_digest: str
    canonical_payload: MappingProxyType
    created_at: datetime

    def __post_init__(self) -> None:
        if any(type(value) is not UUID for value in (self.id, self.runtime_execution_id)):
            raise InvalidTaskInput("Runtime handle snapshot identity is invalid")
        if (
            type(self.tenant_id) is not str
            or not self.tenant_id.strip()
            or len(self.tenant_id) > 128
            or type(self.handle_digest) is not str
            or _DIGEST.fullmatch(self.handle_digest) is None
            or type(self.created_at) is not datetime
            or self.created_at.tzinfo is None
        ):
            raise InvalidTaskInput("Runtime handle snapshot is invalid")
        payload = snapshot_payload(self.canonical_payload, limit=65_536)
        handle = parse_handle_payload(payload)
        if (
            str(self.runtime_execution_id) != handle.runtime_execution_id
            or self.handle_digest != canonical_digest(handle.to_dict())
        ):
            raise InvalidTaskInput("Runtime handle snapshot identity does not match payload")
        object.__setattr__(self, "canonical_payload", payload)


def snapshot_payload(value: Any, *, limit: int) -> MappingProxyType:
    """Freeze and validate a JCS-compatible JSON object at a byte boundary."""
    if type(value) not in (dict, MappingProxyType):
        raise InvalidTaskInput("Runtime snapshot payload must be an object")

    def check_shape(item: Any, depth: int = 0) -> None:
        if depth > 32:
            raise InvalidTaskInput("Runtime snapshot payload is too deep")
        if type(item) in (dict, MappingProxyType):
            for key, child in item.items():
                if type(key) is not str:
                    raise InvalidTaskInput("Runtime snapshot object key is invalid")
                check_shape(child, depth + 1)
            return
        if type(item) in (list, tuple):
            for child in item:
                check_shape(child, depth + 1)

    check_shape(value)
    normalized = _thaw_json(value)
    try:
        encoded = canonical_json_bytes(normalized)
        normalized = decode_json(encoded)
    except (CanonicalizationError, TypeError, ValueError) as exc:
        raise InvalidTaskInput("Runtime snapshot payload is not canonical JSON") from exc
    if len(encoded) > limit:
        raise InvalidTaskInput("Runtime snapshot payload exceeds its byte limit")
    return _freeze_json(normalized)


def parse_assignment_payload(value: Any) -> RuntimeAssignment:
    try:
        return RuntimeAssignment.from_dict(_thaw_json(value))
    except (RuntimeContractError, TypeError, ValueError) as exc:
        raise InvalidTaskInput("Runtime Assignment snapshot payload is invalid") from exc


def parse_handle_payload(value: Any) -> RuntimeExecutionHandle:
    try:
        return RuntimeExecutionHandle.from_dict(_thaw_json(value))
    except (RuntimeContractError, TypeError, ValueError) as exc:
        raise InvalidTaskInput("Runtime handle snapshot payload is invalid") from exc


def _freeze_json(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is dict:
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    if type(value) is list:
        return [_thaw_json(item) for item in value]
    return value


__all__ = ["RuntimeAssignmentSnapshot", "RuntimeHandleSnapshot", "snapshot_payload"]
