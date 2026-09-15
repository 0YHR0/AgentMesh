"""Immutable contracts for coordinated Runtime delivery acquisition.

This module deliberately contains no repository, unit-of-work, worker, or
adapter code.  It is the small value-object boundary shared by the eventual
aggregate-aware acquisition service and its callers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from agentmesh.application.ports import WorkflowWorkItem
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import RunRole
from agentmesh.runtime_sdk import RuntimeExecutionHandle, RuntimeObservation, canonical_digest
from agentmesh.runtime_sdk.canonical import canonical_json_bytes, decode_json
from agentmesh.runtime_sdk.common import _reject_secrets

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_REASON_BYTES = 1024
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_MAX_RESULT_REASON_BYTES = 256
_MAX_WORK_ITEM_BYTES = 262_144
_MAX_TENANT_BYTES = 256
_PLAN_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CROSSED_ACTIVE_PHASES = frozenset(
    {
        RuntimeExecutionPhase.DISPATCHING,
        RuntimeExecutionPhase.ACCEPTED,
        RuntimeExecutionPhase.RUNNING,
        RuntimeExecutionPhase.WAITING_INPUT,
        RuntimeExecutionPhase.WAITING_APPROVAL,
        RuntimeExecutionPhase.PAUSE_REQUESTED,
        RuntimeExecutionPhase.PAUSED,
        RuntimeExecutionPhase.CANCEL_REQUESTED,
    }
)


def _invalid(name: str, detail: str = "is invalid") -> InvalidTaskInput:
    return InvalidTaskInput(f"Coordinated delivery {name} {detail}")


def _uuid(value: Any, name: str, *, optional: bool = False) -> UUID | None:
    if optional and value is None:
        return None
    if type(value) is not UUID:
        raise _invalid(name)
    return value


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _invalid(name, "must be a lowercase 64-hex SHA-256 digest")
    return value


def _plan_digest(value: Any) -> str:
    if type(value) is not str or _PLAN_DIGEST.fullmatch(value) is None:
        raise _invalid("task_plan_digest", "must be sha256:<64 lowercase hex characters>")
    return value


def _exact_int(value: Any, name: str, *, minimum: int = 0) -> int:
    # bool is an int subclass, but is never a valid protocol integer.
    if type(value) is not int or value < minimum:
        raise _invalid(name)
    return value


def _utc(value: Any, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise _invalid(name, "must be an aware UTC datetime")
    return value


def _text(value: Any, name: str, *, max_bytes: int, nonempty: bool = True) -> str:
    if type(value) is not str or (nonempty and not value.strip()):
        raise _invalid(name)
    if len(value.encode("utf-8")) > max_bytes:
        raise _invalid(name, f"must be at most {max_bytes} UTF-8 bytes")
    return value


def _reject_secret_text(value: str, name: str) -> None:
    lowered = value.casefold()
    markers = ("secret", "password", "bearer ", "api_key", "access_token")
    if any(marker in lowered for marker in markers):
        raise _invalid(name, "contains secret material")


def _receipt_uuid(value: Any, name: str) -> UUID:
    if type(value) is str:
        try:
            parsed = UUID(value)
            if str(parsed) == value:
                return parsed
        except ValueError:
            pass
    raise _invalid(name)


def _tenant(value: Any) -> str:
    normalized = _text(value, "tenant_id", max_bytes=_MAX_TENANT_BYTES)
    if normalized != normalized.strip():
        raise _invalid("tenant_id", "must not contain surrounding whitespace")
    return normalized


def _freeze_json(value: Any) -> Any:
    """Recursively freeze ordinary JSON containers for defensive ownership."""
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list or type(value) is tuple:
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple or type(value) is list:
        return [_thaw_json(item) for item in value]
    return value


def _copy_work_item(value: Any) -> WorkflowWorkItem:
    if type(value) is not WorkflowWorkItem:
        raise _invalid("work_item", "must be a WorkflowWorkItem")
    objective = _text(value.objective, "work_item.objective", max_bytes=65_536)
    if not isinstance(value.input, Mapping):
        raise _invalid("work_item.input", "must be a dict")
    # Canonical round-tripping both validates JSON and severs all caller-owned
    # references before the value is frozen.
    try:
        encoded = canonical_json_bytes({"objective": objective, "input": _thaw_json(value.input)})
        if len(encoded) > _MAX_WORK_ITEM_BYTES:
            raise _invalid("work_item", "exceeds the 256 KiB limit")
        from agentmesh.runtime_sdk.canonical import decode_json

        copied = decode_json(encoded)
    except InvalidTaskInput:
        raise
    except Exception as exc:
        raise _invalid("work_item", "must contain canonical JSON") from exc
    return WorkflowWorkItem(
        objective=copied["objective"],
        input=_freeze_json(copied["input"]),
    )


def canonical_work_item_bytes(work_item: WorkflowWorkItem) -> bytes:
    """Return the exact canonical bytes used by delivery assignment identity."""
    copied = _copy_work_item(work_item)
    # Explicitly materialize the projection; canonical.py's dataclass helper
    # cannot deepcopy MappingProxyType safely on every supported Python.
    return canonical_json_bytes({"objective": copied.objective, "input": _thaw_json(copied.input)})


def assignment_projection_payload(
    *,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    subtask_id: UUID | None,
    role: RunRole,
    runtime_version_id: UUID,
    runtime_execution_intent_id: UUID,
    agent_version_id: UUID,
    agent_version_digest: str,
    task_plan_version: int,
    task_plan_digest: str,
    run_revision: int,
    work_item: WorkflowWorkItem,
) -> dict[str, Any]:
    """Build the stable projection; ownership-only lease fields are absent."""
    tenant_id = _tenant(tenant_id)
    for value, name in (
        (task_id, "task_id"),
        (run_id, "run_id"),
        (runtime_version_id, "runtime_version_id"),
        (runtime_execution_intent_id, "runtime_execution_intent_id"),
        (agent_version_id, "agent_version_id"),
    ):
        _uuid(value, name)
    _uuid(subtask_id, "subtask_id", optional=True)
    if type(role) is not RunRole or role not in {RunRole.EXECUTOR, RunRole.SUPERVISOR}:
        raise _invalid("role")
    if role is RunRole.SUPERVISOR and subtask_id is not None:
        raise _invalid("subtask_id", "must be None for a Supervisor")
    if role is RunRole.EXECUTOR and subtask_id is None:
        raise _invalid("subtask_id", "must be a UUID for an Executor")
    _digest(agent_version_digest, "agent_version_digest")
    _exact_int(task_plan_version, "task_plan_version", minimum=1)
    _plan_digest(task_plan_digest)
    _exact_int(run_revision, "run_revision")
    copied = _copy_work_item(work_item)
    return {
        "tenant_id": tenant_id,
        "task_id": str(task_id),
        "run_id": str(run_id),
        "subtask_id": str(subtask_id) if subtask_id is not None else None,
        "role": role.value,
        "runtime_version_id": str(runtime_version_id),
        "runtime_execution_intent_id": str(runtime_execution_intent_id),
        "agent_version_id": str(agent_version_id),
        "agent_version_digest": agent_version_digest,
        "task_plan_version": task_plan_version,
        "task_plan_digest": task_plan_digest,
        "run_revision": run_revision,
        "work_item": {
            "objective": copied.objective,
            "input": _thaw_json(copied.input),
        },
    }


def assignment_projection_digest(**kwargs: Any) -> str:
    """Hash only stable assignment identity (never attempt/lease ownership)."""
    return canonical_digest(assignment_projection_payload(**kwargs))


def ownership_digest(
    *,
    assignment_projection_digest: str,
    tenant_id: str,
    task_id: UUID,
    run_id: UUID,
    subtask_id: UUID | None,
    attempt_id: UUID,
    fencing_token: int,
    lease_token: UUID,
    lease_deadline: datetime,
) -> str:
    """Hash lease ownership and bind it to the stable assignment projection."""
    _digest(assignment_projection_digest, "assignment_projection_digest")
    tenant_id = _tenant(tenant_id)
    for value, name in ((task_id, "task_id"), (run_id, "run_id"), (attempt_id, "attempt_id")):
        _uuid(value, name)
    _uuid(subtask_id, "subtask_id", optional=True)
    _exact_int(fencing_token, "fencing_token", minimum=1)
    _uuid(lease_token, "lease_token")
    _utc(lease_deadline, "lease_deadline")
    return canonical_digest(
        {
            "assignment_projection_digest": assignment_projection_digest,
            "tenant_id": tenant_id,
            "task_id": str(task_id),
            "run_id": str(run_id),
            "subtask_id": str(subtask_id) if subtask_id is not None else None,
            "attempt_id": str(attempt_id),
            "fencing_token": fencing_token,
            "lease_token": str(lease_token),
            "lease_deadline": lease_deadline,
        }
    )


def stable_dispatch_identity(
    tenant_id: str, execution_id: UUID, assignment_digest: str
) -> tuple[str, str]:
    """Return the canonical provider dispatch key and digest.

    This identity is shared by receipt normalization and the aggregate
    dispatch command.  Keeping it in the pure delivery contract prevents
    evidence consumers from depending on a dispatch service private helper.
    """
    tenant_id = _tenant(tenant_id)
    _uuid(execution_id, "execution_id")
    _digest(assignment_digest, "assignment_digest")
    dispatch_key = f"runtime-dispatch:{tenant_id}:{execution_id}"
    return dispatch_key, canonical_digest(
        {
            "execution_id": str(execution_id),
            "dispatch_key": dispatch_key,
            "assignment_digest": assignment_digest,
        }
    )


@dataclass(frozen=True)
class CoordinatedDeliveryLeaseV1:
    """A fully detached, immutable lease usable by a delivery caller."""

    schema_version: int
    tenant_id: str
    task_id: UUID
    run_id: UUID
    subtask_id: UUID | None
    attempt_id: UUID
    role: RunRole
    fencing_token: int
    lease_token: UUID
    lease_deadline: datetime
    runtime_version_id: UUID
    runtime_execution_intent_id: UUID
    task_plan_version: int
    task_plan_digest: str
    run_revision: int
    agent_version_id: UUID
    agent_version_digest: str
    work_item: WorkflowWorkItem
    assignment_projection_digest: str
    ownership_digest: str

    def __post_init__(self) -> None:
        _exact_int(self.schema_version, "schema_version", minimum=1)
        if self.schema_version != 1:
            raise _invalid("schema_version", "must be 1")
        payload = assignment_projection_payload(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            subtask_id=self.subtask_id,
            role=self.role,
            runtime_version_id=self.runtime_version_id,
            runtime_execution_intent_id=self.runtime_execution_intent_id,
            agent_version_id=self.agent_version_id,
            agent_version_digest=self.agent_version_digest,
            task_plan_version=self.task_plan_version,
            task_plan_digest=self.task_plan_digest,
            run_revision=self.run_revision,
            work_item=self.work_item,
        )
        _uuid(self.attempt_id, "attempt_id")
        _exact_int(self.fencing_token, "fencing_token", minimum=1)
        _uuid(self.lease_token, "lease_token")
        _utc(self.lease_deadline, "lease_deadline")
        _digest(self.assignment_projection_digest, "assignment_projection_digest")
        _digest(self.ownership_digest, "ownership_digest")
        expected_assignment = canonical_digest(payload)
        if self.assignment_projection_digest != expected_assignment:
            raise _invalid("assignment_projection_digest", "does not match the stable projection")
        expected_ownership = ownership_digest(
            assignment_projection_digest=expected_assignment,
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            subtask_id=self.subtask_id,
            attempt_id=self.attempt_id,
            fencing_token=self.fencing_token,
            lease_token=self.lease_token,
            lease_deadline=self.lease_deadline,
        )
        if self.ownership_digest != expected_ownership:
            raise _invalid("ownership_digest", "does not match lease ownership")
        copied = _copy_work_item(self.work_item)
        object.__setattr__(self, "work_item", copied)


@dataclass(frozen=True)
class CoordinatedDispatchReceiptV1:
    """Immutable, provider-safe evidence returned by a coordinated dispatch.

    The lease remains the authority for mutable ownership. Lease ownership
    fields never enter provider receipt identity, so an Attempt replacement
    can safely replay the same provider receipt.
    """

    schema_version: int
    dispatch_digest: str
    assignment_digest: str
    runtime_execution_id: UUID
    assignment_id: UUID
    provider_execution_ref: str | None = None
    provider_generation: str | None = None
    handle: RuntimeExecutionHandle | None = None
    observation: RuntimeObservation | None = None
    receipt_digest: str | None = None

    schema_name = "agentmesh.coordinated-dispatch-receipt"

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise _invalid("schema_version", "must be 1")
        _digest(self.dispatch_digest, "dispatch_digest")
        _digest(self.assignment_digest, "assignment_digest")
        for value, name in (
            (self.runtime_execution_id, "runtime_execution_id"),
            (self.assignment_id, "assignment_id"),
        ):
            _uuid(value, name)
        if self.provider_execution_ref is not None:
            _text(
                self.provider_execution_ref,
                "provider_execution_ref",
                max_bytes=4096,
            )
            _reject_secret_text(self.provider_execution_ref, "provider_execution_ref")
        if self.provider_generation is not None:
            _text(self.provider_generation, "provider_generation", max_bytes=256)
            _reject_secret_text(self.provider_generation, "provider_generation")
        if type(self.handle) not in {RuntimeExecutionHandle, type(None)}:
            raise _invalid("handle", "must be a RuntimeExecutionHandle")
        if type(self.observation) not in {RuntimeObservation, type(None)}:
            raise _invalid("observation", "must be a RuntimeObservation")
        if self.handle is None and self.observation is None:
            raise _invalid("payload", "requires a handle or observation")
        if self.handle is not None:
            object.__setattr__(
                self,
                "handle",
                RuntimeExecutionHandle.from_dict(
                    decode_json(canonical_json_bytes(self.handle.to_dict()))
                ),
            )
        if self.observation is not None:
            copied_observation = RuntimeObservation.from_dict(
                decode_json(
                    canonical_json_bytes(_thaw_json(self.observation.to_dict()))
                )
            )
            for field_name in ("progress", "usage", "extensions", "output"):
                object.__setattr__(
                    copied_observation,
                    field_name,
                    _freeze_json(getattr(copied_observation, field_name)),
                )
            object.__setattr__(
                copied_observation,
                "governed_action_requests",
                tuple(
                    _freeze_json(item)
                    for item in copied_observation.governed_action_requests
                ),
            )
            object.__setattr__(
                self,
                "observation",
                copied_observation,
            )
        if self.handle is not None:
            _reject_secret_text(self.handle.provider_execution_ref, "handle.provider_execution_ref")
            if self.handle.provider_generation is not None:
                _reject_secret_text(self.handle.provider_generation, "handle.provider_generation")
            if (
                self.handle.runtime_execution_id != str(self.runtime_execution_id)
                or self.handle.assignment_id != str(self.assignment_id)
                or self.handle.assignment_digest != self.assignment_digest
            ):
                raise _invalid("handle", "identity does not match the receipt")
            if self.provider_execution_ref is not None and (
                self.handle.provider_execution_ref != self.provider_execution_ref
            ):
                raise _invalid("provider_execution_ref", "does not match the handle")
            if self.provider_generation is not None and (
                self.handle.provider_generation != self.provider_generation
            ):
                raise _invalid("provider_generation", "does not match the handle")
        if self.observation is not None:
            if (
                self.observation.runtime_execution_id != str(self.runtime_execution_id)
                or self.observation.assignment_id != str(self.assignment_id)
                or self.observation.assignment_digest != self.assignment_digest
            ):
                raise _invalid("observation", "identity does not match the receipt")
        payload = self._digest_payload()
        _reject_secrets(payload, path="coordinated_dispatch_receipt")
        if len(canonical_json_bytes(payload)) > 65_536:
            raise _invalid("payload", "exceeds the 64 KiB limit")
        expected = canonical_digest(payload)
        if self.receipt_digest is None:
            object.__setattr__(self, "receipt_digest", expected)
        elif self.receipt_digest != expected:
            raise _invalid("receipt_digest", "does not match the canonical receipt")

    def _digest_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "dispatch_digest": self.dispatch_digest,
            "assignment_digest": self.assignment_digest,
            "runtime_execution_id": str(self.runtime_execution_id),
            "assignment_id": str(self.assignment_id),
        }
        optional: dict[str, Any] = {
            "provider_execution_ref": self.provider_execution_ref,
            "provider_generation": self.provider_generation,
            "handle": _thaw_json(self.handle.to_dict()) if self.handle is not None else None,
            "observation": (
                _thaw_json(self.observation.to_dict())
                if self.observation is not None
                else None
            ),
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload = self._digest_payload()
        payload["receipt_digest"] = self.receipt_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CoordinatedDispatchReceiptV1:
        if type(value) is not dict:
            raise _invalid("receipt", "must be an object")
        allowed = {
            "schema_name",
            "schema_version",
            "dispatch_digest",
            "assignment_digest",
            "runtime_execution_id",
            "assignment_id",
            "provider_execution_ref",
            "provider_generation",
            "handle",
            "observation",
            "receipt_digest",
        }
        if value.get("schema_name") != cls.schema_name or set(value) - allowed:
            raise _invalid("schema", "is not closed")
        if value.get("schema_version") != 1:
            raise _invalid("schema_version", "must be 1")
        if "receipt_digest" not in value:
            raise _invalid("receipt_digest", "is required")
        return cls(
            schema_version=value.get("schema_version"),
            dispatch_digest=value.get("dispatch_digest"),
            assignment_digest=value.get("assignment_digest"),
            runtime_execution_id=_receipt_uuid(
                value.get("runtime_execution_id"), "runtime_execution_id"
            ),
            assignment_id=_receipt_uuid(value.get("assignment_id"), "assignment_id"),
            provider_execution_ref=value.get("provider_execution_ref"),
            provider_generation=value.get("provider_generation"),
            handle=(
                RuntimeExecutionHandle.from_dict(value["handle"])
                if value.get("handle") is not None
                else None
            ),
            observation=(
                RuntimeObservation.from_dict(value["observation"])
                if value.get("observation") is not None
                else None
            ),
            receipt_digest=value.get("receipt_digest"),
        )


@dataclass(frozen=True)
class RecoveryCrossedProof:
    """Proof that an expired owner had crossed the provider dispatch boundary."""

    execution_id: UUID
    expired_owner_attempt_id: UUID
    expired_owner_fencing_token: int
    phase: RuntimeExecutionPhase
    version: int
    # A crossed proof is only useful when it is tied to the exact immutable
    # Assignment authority that was dispatched.  These are deliberately
    # required: recovery must never synthesize an Assignment identity.
    assignment_id: UUID
    assignment_digest: str
    persisted_handle_snapshot_id: UUID | None = None
    persisted_handle_snapshot_digest: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.execution_id, "execution_id")
        _uuid(self.expired_owner_attempt_id, "expired_owner_attempt_id")
        _exact_int(self.expired_owner_fencing_token, "expired_owner_fencing_token", minimum=1)
        if (
            type(self.phase) is not RuntimeExecutionPhase
            or self.phase not in _CROSSED_ACTIVE_PHASES
        ):
            raise _invalid("phase", "must be a crossed active Runtime phase")
        _exact_int(self.version, "version", minimum=1)
        _uuid(self.assignment_id, "assignment_id")
        _digest(self.assignment_digest, "assignment_digest")
        _uuid(self.persisted_handle_snapshot_id, "persisted_handle_snapshot_id", optional=True)
        if self.persisted_handle_snapshot_id is None:
            if self.persisted_handle_snapshot_digest is not None:
                raise _invalid("persisted_handle_snapshot_digest", "requires snapshot identity")
        else:
            _digest(self.persisted_handle_snapshot_digest, "persisted_handle_snapshot_digest")


class CoordinatedDeliveryResultKind(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ACQUIRED = "ACQUIRED"
    RECOVERED_PRE_BOUNDARY = "RECOVERED_PRE_BOUNDARY"
    RECOVER_CROSSED = "RECOVER_CROSSED"
    IN_PROGRESS = "IN_PROGRESS"
    REPLAY_PROCESSED = "REPLAY_PROCESSED"
    BLOCKED_BY_DRAIN = "BLOCKED_BY_DRAIN"
    WAITING_APPROVAL = "WAITING_APPROVAL"


@dataclass(frozen=True)
class CoordinatedDeliveryResult:
    """Closed result union; only acquisition branches can carry a usable lease."""

    kind: CoordinatedDeliveryResultKind
    lease: CoordinatedDeliveryLeaseV1 | None = None
    recovery_crossed_proof: RecoveryCrossedProof | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not CoordinatedDeliveryResultKind:
            raise _invalid("result kind")
        if self.reason is not None:
            _text(self.reason, "result.reason", max_bytes=_MAX_REASON_BYTES)
        lease_kinds = {
            CoordinatedDeliveryResultKind.ACQUIRED,
            CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY,
        }
        if self.kind in lease_kinds:
            if type(self.lease) is not CoordinatedDeliveryLeaseV1:
                raise _invalid("result payload", "requires a lease")
            if self.recovery_crossed_proof is not None:
                raise _invalid("result payload", "cannot include recovery proof")
            if self.reason is not None:
                raise _invalid("result.reason", "is not valid for a lease result")
        elif self.kind is CoordinatedDeliveryResultKind.RECOVER_CROSSED:
            if type(self.recovery_crossed_proof) is not RecoveryCrossedProof:
                raise _invalid("result payload", "requires a recovery-crossed proof")
            if self.lease is not None:
                raise _invalid("result payload", "cannot include a usable lease")
            if self.reason is not None:
                raise _invalid("result.reason", "is not valid for RECOVER_CROSSED")
        elif self.lease is not None or self.recovery_crossed_proof is not None:
            raise _invalid("result payload", "cannot carry a lease or recovery proof")

    @classmethod
    def acquired(cls, lease: CoordinatedDeliveryLeaseV1) -> CoordinatedDeliveryResult:
        return cls(CoordinatedDeliveryResultKind.ACQUIRED, lease=lease)

    @classmethod
    def recovered_pre_boundary(cls, lease: CoordinatedDeliveryLeaseV1) -> CoordinatedDeliveryResult:
        return cls(CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY, lease=lease)

    @classmethod
    def recover_crossed(cls, proof: RecoveryCrossedProof) -> CoordinatedDeliveryResult:
        return cls(CoordinatedDeliveryResultKind.RECOVER_CROSSED, recovery_crossed_proof=proof)

    @classmethod
    def without_lease(
        cls, kind: CoordinatedDeliveryResultKind, *, reason: str | None = None
    ) -> CoordinatedDeliveryResult:
        if kind in {
            CoordinatedDeliveryResultKind.ACQUIRED,
            CoordinatedDeliveryResultKind.RECOVERED_PRE_BOUNDARY,
            CoordinatedDeliveryResultKind.RECOVER_CROSSED,
        }:
            raise _invalid("result kind", "requires a typed payload")
        return cls(kind, reason=reason)


class CoordinatedRuntimeDeliveryResultKind(str, Enum):
    """The only successful outcomes exposed by the delivery orchestrator.

    ``NOT_APPLICABLE`` is deliberately retained as a routing-only value.  A
    caller that receives it must hand the envelope to the legacy path; it is
    not a terminal delivery result.
    """

    NOT_APPLICABLE = "NOT_APPLICABLE"
    PROCESSED = "PROCESSED"
    REPLAY = "REPLAY"
    BLOCKED_BY_DRAIN = "BLOCKED_BY_DRAIN"
    PARKED_UNKNOWN = "PARKED_UNKNOWN"


def _result_uuid(value: Any, name: str, *, optional: bool = False) -> UUID | None:
    """Parse only canonical UUID text at the serialization boundary."""
    if optional and value is None:
        return None
    if type(value) is not str:
        raise _invalid(name, "must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise _invalid(name, "must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise _invalid(name, "must be a canonical UUID string")
    return parsed


def _safe_result_reason(value: Any) -> str:
    if (
        type(value) is not str
        or not _SAFE_REASON.fullmatch(value)
        or len(value.encode("utf-8")) > _MAX_RESULT_REASON_BYTES
    ):
        raise _invalid("result.reason", "must be a bounded safe code")
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in ("secret", "password", "bearer ", "api_key", "access_token")
    ):
        raise _invalid("result.reason", "contains secret material")
    return value


@dataclass(frozen=True)
class CoordinatedRuntimeDeliveryResult:
    """Safe public result for one coordinated delivery attempt.

    This DTO intentionally contains identities only.  In particular, provider
    receipts, observations, handles, and exception text cannot cross this
    boundary.  The optional ownership fields let replay and routing results
    avoid inventing an Attempt or Runtime execution identity.
    """

    kind: CoordinatedRuntimeDeliveryResultKind
    tenant_id: str
    task_id: UUID
    run_id: UUID
    attempt_id: UUID | None = None
    execution_id: UUID | None = None
    reason: str | None = None

    schema_name = "agentmesh.coordinated-runtime-delivery-result"
    schema_version = 1

    def __post_init__(self) -> None:
        if type(self.kind) is not CoordinatedRuntimeDeliveryResultKind:
            raise _invalid("result.kind")
        _tenant(self.tenant_id)
        _uuid(self.task_id, "result.task_id")
        _uuid(self.run_id, "result.run_id")
        _uuid(self.attempt_id, "result.attempt_id", optional=True)
        _uuid(self.execution_id, "result.execution_id", optional=True)
        if self.reason is not None:
            _safe_result_reason(self.reason)
        if (
            self.kind
            in {
                CoordinatedRuntimeDeliveryResultKind.NOT_APPLICABLE,
                CoordinatedRuntimeDeliveryResultKind.REPLAY,
            }
            and any(
                value is not None for value in (self.attempt_id, self.execution_id, self.reason)
            )
        ):
            raise _invalid("result payload", "routing/replay results cannot carry ownership")
        if self.kind is CoordinatedRuntimeDeliveryResultKind.BLOCKED_BY_DRAIN and any(
            value is not None for value in (self.attempt_id, self.execution_id)
        ):
            raise _invalid("result payload", "blocked result cannot carry ownership")
        if self.kind is CoordinatedRuntimeDeliveryResultKind.PROCESSED:
            if self.attempt_id is None:
                raise _invalid("result.attempt_id", "is required for PROCESSED")
            if self.reason is not None:
                raise _invalid("result.reason", "is not valid for PROCESSED")
        if self.kind is CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN:
            if self.attempt_id is None or self.execution_id is None:
                raise _invalid("result payload", "PARKED_UNKNOWN requires ownership")
            if self.reason is None:
                raise _invalid("result.reason", "is required for PARKED_UNKNOWN")

    @property
    def runtime_execution_id(self) -> UUID | None:
        """Compatibility spelling used by the persisted Runtime contracts."""
        return self.execution_id

    @classmethod
    def not_applicable(
        cls, *, tenant_id: str, task_id: UUID, run_id: UUID
    ) -> CoordinatedRuntimeDeliveryResult:
        return cls(
            kind=CoordinatedRuntimeDeliveryResultKind.NOT_APPLICABLE,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
        )

    @classmethod
    def processed(
        cls,
        *,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        execution_id: UUID | None = None,
    ) -> CoordinatedRuntimeDeliveryResult:
        return cls(
            kind=CoordinatedRuntimeDeliveryResultKind.PROCESSED,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            execution_id=execution_id,
        )

    @classmethod
    def replay(
        cls, *, tenant_id: str, task_id: UUID, run_id: UUID
    ) -> CoordinatedRuntimeDeliveryResult:
        return cls(
            kind=CoordinatedRuntimeDeliveryResultKind.REPLAY,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
        )

    @classmethod
    def blocked_by_drain(
        cls, *, tenant_id: str, task_id: UUID, run_id: UUID, reason: str | None = None
    ) -> CoordinatedRuntimeDeliveryResult:
        return cls(
            kind=CoordinatedRuntimeDeliveryResultKind.BLOCKED_BY_DRAIN,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            reason=reason,
        )

    @classmethod
    def parked_unknown(
        cls,
        *,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        execution_id: UUID,
        reason: str,
    ) -> CoordinatedRuntimeDeliveryResult:
        return cls(
            kind=CoordinatedRuntimeDeliveryResultKind.PARKED_UNKNOWN,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            execution_id=execution_id,
            reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the closed, canonical JSON-compatible representation."""
        result: dict[str, Any] = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "tenant_id": self.tenant_id,
            "task_id": str(self.task_id),
            "run_id": str(self.run_id),
        }
        for name, value in (
            ("attempt_id", self.attempt_id),
            ("execution_id", self.execution_id),
            ("reason", self.reason),
        ):
            if value is not None:
                result[name] = str(value) if isinstance(value, UUID) else value
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CoordinatedRuntimeDeliveryResult:
        if type(value) is not dict:
            raise _invalid("result", "must be an object")
        allowed = {
            "schema_name",
            "schema_version",
            "kind",
            "tenant_id",
            "task_id",
            "run_id",
            "attempt_id",
            "execution_id",
            "reason",
        }
        if value.get("schema_name") != cls.schema_name or set(value) - allowed:
            raise _invalid("result schema", "is not closed")
        if value.get("schema_version") != cls.schema_version:
            raise _invalid("result.schema_version", "must be 1")
        try:
            kind = CoordinatedRuntimeDeliveryResultKind(value.get("kind"))
        except (TypeError, ValueError) as exc:
            raise _invalid("result.kind") from exc
        reason = value.get("reason")
        return cls(
            kind=kind,
            tenant_id=value.get("tenant_id"),
            task_id=_result_uuid(value.get("task_id"), "result.task_id"),  # type: ignore[arg-type]
            run_id=_result_uuid(value.get("run_id"), "result.run_id"),  # type: ignore[arg-type]
            attempt_id=_result_uuid(value.get("attempt_id"), "result.attempt_id", optional=True),
            execution_id=_result_uuid(
                value.get("execution_id"), "result.execution_id", optional=True
            ),
            reason=reason,
        )


class DeliveryInProgress(RuntimeError):
    """Retryable ownership outcome, never a successful delivery result."""

    def __init__(self, task_id: UUID | None = None, run_id: UUID | None = None) -> None:
        _uuid(task_id, "delivery_in_progress.task_id", optional=True)
        _uuid(run_id, "delivery_in_progress.run_id", optional=True)
        if (task_id is None) != (run_id is None):
            raise _invalid("delivery_in_progress", "task_id and run_id must be paired")
        self.task_id = task_id
        self.run_id = run_id
        super().__init__("coordinated runtime delivery is already in progress")


__all__ = [
    "CoordinatedDispatchReceiptV1",
    "CoordinatedDeliveryLeaseV1",
    "CoordinatedDeliveryResult",
    "CoordinatedDeliveryResultKind",
    "CoordinatedRuntimeDeliveryResult",
    "CoordinatedRuntimeDeliveryResultKind",
    "DeliveryInProgress",
    "RecoveryCrossedProof",
    "assignment_projection_digest",
    "assignment_projection_payload",
    "canonical_work_item_bytes",
    "ownership_digest",
    "stable_dispatch_identity",
]
