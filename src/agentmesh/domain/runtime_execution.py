"""Framework-neutral runtime registry and execution state rules.

This module deliberately contains no adapter, provider, SQLAlchemy, or Runtime SDK imports.
Provider observations are evidence; only an application command may advance Task/Run/Attempt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeRegistrationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class RuntimeVisibility(str, Enum):
    PLATFORM = "platform"
    TENANT = "tenant"
    PRIVATE = "private"


class RuntimeVersionStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"
    REVOKED = "REVOKED"


class RuntimeTrustProfile(str, Enum):
    BUILT_IN = "built_in"
    TRUSTED_PROCESS = "trusted_process"
    ISOLATED = "isolated"
    REMOTE = "remote"


class RuntimeExecutionPhase(str, Enum):
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


class RuntimeObservationOutcome(str, Enum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    GAP = "GAP"
    STALE_OWNER = "STALE_OWNER"
    CONFLICT = "CONFLICT"


class RuntimeLifecycleOperation(str, Enum):
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"


class RuntimeLifecycleStatus(str, Enum):
    REQUESTED = "REQUESTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class RuntimeObservationEvidence:
    """Safe immutable projection of one received provider observation.

    The projection deliberately contains no provider body or opaque handle.  The
    raw evidence column is an internal persistence detail and never crosses the
    application repository boundary.
    """

    id: UUID
    tenant_id: str
    runtime_execution_id: UUID
    observation_id: str
    observation_digest: str
    assignment_id: UUID
    assignment_digest: str
    provider_sequence: int | None
    phase: RuntimeExecutionPhase
    observed_at: datetime
    received_at: datetime
    safe_summary: str | None
    processing_outcome: RuntimeObservationOutcome
    provider_event_present: bool

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID
            for value in (self.id, self.runtime_execution_id, self.assignment_id)
        ):
            raise InvalidTaskInput("Runtime observation identity is invalid")
        if (
            type(self.tenant_id) is not str
            or not self.tenant_id.strip()
            or type(self.observation_id) is not str
            or not self.observation_id.strip()
            or len(self.observation_id) > 512
            or type(self.observation_digest) is not str
            or _DIGEST.fullmatch(self.observation_digest) is None
            or type(self.assignment_digest) is not str
            or _DIGEST.fullmatch(self.assignment_digest) is None
            or type(self.phase) is not RuntimeExecutionPhase
            or type(self.processing_outcome) is not RuntimeObservationOutcome
            or type(self.provider_event_present) is not bool
            or type(self.observed_at) is not datetime
            or type(self.received_at) is not datetime
            or type(self.provider_sequence) not in (int, type(None))
            or (self.provider_sequence is not None and self.provider_sequence < 0)
        ):
            raise InvalidTaskInput("Runtime observation is invalid")
        if self.safe_summary is not None and (
            type(self.safe_summary) is not str or len(self.safe_summary) > 4096
        ):
            raise InvalidTaskInput("Runtime observation summary is invalid")


@dataclass(frozen=True)
class RuntimeLifecycleIntent:
    """Safe projection of a persisted lifecycle command intent."""

    id: UUID
    tenant_id: str
    runtime_execution_id: UUID
    operation_id: str
    operation: RuntimeLifecycleOperation
    intent_digest: str
    status: RuntimeLifecycleStatus
    deadline: datetime
    receipt_summary: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID
            for value in (self.id, self.runtime_execution_id)
        ) or type(self.tenant_id) is not str or not self.tenant_id.strip():
            raise InvalidTaskInput("Runtime lifecycle identity is invalid")
        if (
            type(self.operation_id) is not str
            or not self.operation_id.strip()
            or len(self.operation_id) > 512
            or type(self.operation) is not RuntimeLifecycleOperation
            or type(self.intent_digest) is not str
            or _DIGEST.fullmatch(self.intent_digest) is None
            or type(self.status) is not RuntimeLifecycleStatus
            or type(self.version) is not int
            or self.version < 1
            or type(self.receipt_summary) not in (str, type(None))
            or (self.receipt_summary is not None and len(self.receipt_summary) > 4096)
        ):
            raise InvalidTaskInput("Runtime lifecycle intent is invalid")


@dataclass(frozen=True)
class ReattachEvidence:
    execution_id: UUID
    assignment_digest: str
    provider_execution_ref: str
    inspected_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.execution_id) is not UUID
            or type(self.assignment_digest) is not str
            or _DIGEST.fullmatch(self.assignment_digest) is None
            or type(self.provider_execution_ref) is not str
            or not self.provider_execution_ref
            or len(self.provider_execution_ref) > 4096
            or type(self.inspected_at) is not datetime
            or self.inspected_at.tzinfo is None
        ):
            raise InvalidTaskInput("Runtime reattach evidence is invalid")


@dataclass(frozen=True)
class RuntimeRegistration:
    id: UUID
    tenant_id: str | None
    name: str
    owner_principal_id: UUID
    visibility: RuntimeVisibility
    status: RuntimeRegistrationStatus
    default_version_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        name: str,
        owner_principal_id: UUID,
        visibility: RuntimeVisibility,
        tenant_id: str | None = None,
        registration_id: UUID | None = None,
        now: datetime | None = None,
    ) -> RuntimeRegistration:
        if type(name) is not str:
            raise InvalidTaskInput("Runtime registration identity is invalid")
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 160:
            raise InvalidTaskInput("Runtime registration identity is invalid")
        if type(owner_principal_id) is not UUID or type(visibility) is not RuntimeVisibility:
            raise InvalidTaskInput("Runtime registration identity is invalid")
        if visibility is RuntimeVisibility.PLATFORM and tenant_id is not None:
            raise InvalidTaskInput("Platform Runtime registrations cannot be tenant scoped")
        if visibility is not RuntimeVisibility.PLATFORM and not (tenant_id or "").strip():
            raise InvalidTaskInput("Tenant Runtime registrations require a tenant")
        timestamp = now or utc_now()
        return cls(
            id=registration_id or uuid4(),
            tenant_id=tenant_id.strip() if tenant_id else None,
            name=normalized_name,
            owner_principal_id=owner_principal_id,
            visibility=visibility,
            status=RuntimeRegistrationStatus.ACTIVE,
            default_version_id=None,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def set_default(
        self, version: RuntimeVersion, *, now: datetime | None = None
    ) -> RuntimeRegistration:
        if version.runtime_id != self.id or version.status is not RuntimeVersionStatus.PUBLISHED:
            raise InvalidTaskTransition("Runtime default must be a published compatible version")
        return replace(
            self,
            default_version_id=version.id,
            version=self.version + 1,
            updated_at=now or utc_now(),
        )


@dataclass(frozen=True)
class RuntimeVersion:
    id: UUID
    runtime_id: UUID
    api_version: int
    adapter_kind: str
    artifact_digest: str
    configuration_digest: str
    descriptor: MappingProxyType
    trust_profile: RuntimeTrustProfile
    compatibility: MappingProxyType
    status: RuntimeVersionStatus
    created_at: datetime
    published_at: datetime | None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.id) is not UUID or type(self.runtime_id) is not UUID:
            raise InvalidTaskInput("Runtime Version identity is invalid")
        if type(self.api_version) is not int or self.api_version != 1:
            raise InvalidTaskInput("Runtime API version must be 1")
        if (
            type(self.adapter_kind) is not str
            or not self.adapter_kind.strip()
            or type(self.artifact_digest) is not str
            or type(self.configuration_digest) is not str
            or _DIGEST.fullmatch(self.artifact_digest) is None
            or _DIGEST.fullmatch(self.configuration_digest) is None
        ):
            raise InvalidTaskInput("Runtime Version identity is invalid")
        if (
            type(self.status) is not RuntimeVersionStatus
            or type(self.trust_profile) is not RuntimeTrustProfile
        ):
            raise InvalidTaskInput("Runtime Version policy is invalid")
        if self.status is RuntimeVersionStatus.PUBLISHED and self.published_at is None:
            raise InvalidTaskInput("Published Runtime Version requires publication time")
        if self.status is RuntimeVersionStatus.DRAFT and self.published_at is not None:
            raise InvalidTaskInput("Draft Runtime Version cannot have publication time")
        if type(self.descriptor) is dict:
            object.__setattr__(self, "descriptor", _freeze_json(self.descriptor))
        if type(self.compatibility) is dict:
            object.__setattr__(self, "compatibility", _freeze_json(self.compatibility))
        if (
            type(self.descriptor) is not MappingProxyType
            or type(self.compatibility) is not MappingProxyType
        ):
            raise InvalidTaskInput("Runtime Version snapshots must be mappings")

    def publish(self, *, now: datetime | None = None) -> RuntimeVersion:
        if self.status is not RuntimeVersionStatus.DRAFT:
            raise InvalidTaskTransition("Only a draft Runtime Version can be published")
        return replace(self, status=RuntimeVersionStatus.PUBLISHED, published_at=now or utc_now())


@dataclass(frozen=True)
class RuntimeExecution:
    id: UUID
    tenant_id: str
    run_id: UUID
    runtime_version_id: UUID
    assignment_id: UUID
    assignment_digest: str
    dispatch_key: str
    dispatch_digest: str
    provider_execution_ref: str | None
    provider_generation: str | None
    phase: RuntimeExecutionPhase
    current_owner_attempt_id: UUID | None
    current_fencing_token: int | None
    provider_sequence: int | None
    checkpoint_ref: str | None
    workspace_ref: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None = None

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID
            for value in (self.id, self.run_id, self.runtime_version_id, self.assignment_id)
        ):
            raise InvalidTaskInput("Runtime execution identity is invalid")
        if type(self.phase) is not RuntimeExecutionPhase or type(self.version) is not int:
            raise InvalidTaskInput("Runtime execution state is invalid")
        if self.current_owner_attempt_id is not None and (
            type(self.current_owner_attempt_id) is not UUID
        ):
            raise InvalidTaskInput("Runtime owner identity is invalid")
        if (
            type(self.tenant_id) is not str
            or type(self.dispatch_key) is not str
            or not self.tenant_id.strip()
            or not self.dispatch_key.strip()
            or len(self.dispatch_key) > 512
            or type(self.assignment_digest) is not str
            or type(self.dispatch_digest) is not str
            or _DIGEST.fullmatch(self.assignment_digest) is None
            or _DIGEST.fullmatch(self.dispatch_digest) is None
        ):
            raise InvalidTaskInput("Runtime execution identity is invalid")
        if self.current_fencing_token is not None and type(self.current_fencing_token) is not int:
            raise InvalidTaskInput("Runtime fencing token is invalid")
        if self.provider_sequence is not None and (
            type(self.provider_sequence) is not int or self.provider_sequence < 0
        ):
            raise InvalidTaskInput("Runtime provider sequence is invalid")
        for ref in (
            self.provider_execution_ref,
            self.provider_generation,
            self.checkpoint_ref,
            self.workspace_ref,
        ):
            if ref is not None and (type(ref) is not str or len(ref) > 4096):
                raise InvalidTaskInput("Runtime provider reference is invalid")

    @classmethod
    def prepare(
        cls,
        *,
        tenant_id: str,
        run_id: UUID,
        runtime_version_id: UUID,
        assignment_id: UUID,
        assignment_digest: str,
        dispatch_key: str,
        dispatch_digest: str,
        execution_id: UUID | None = None,
        now: datetime | None = None,
    ) -> RuntimeExecution:
        if (
            type(tenant_id) is not str
            or type(dispatch_key) is not str
            or type(dispatch_digest) is not str
            or not tenant_id.strip()
            or not dispatch_key.strip()
            or type(assignment_digest) is not str
            or _DIGEST.fullmatch(dispatch_digest) is None
            or _DIGEST.fullmatch(assignment_digest) is None
        ):
            raise InvalidTaskInput("Runtime execution identity is invalid")
        timestamp = now or utc_now()
        return cls(
            id=execution_id or uuid4(),
            tenant_id=tenant_id.strip(),
            run_id=run_id,
            runtime_version_id=runtime_version_id,
            assignment_id=assignment_id,
            assignment_digest=assignment_digest,
            dispatch_key=dispatch_key,
            dispatch_digest=dispatch_digest,
            provider_execution_ref=None,
            provider_generation=None,
            phase=RuntimeExecutionPhase.PREPARED,
            current_owner_attempt_id=None,
            current_fencing_token=None,
            provider_sequence=None,
            checkpoint_ref=None,
            workspace_ref=None,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def claim(
        self,
        *,
        attempt_id: UUID,
        fencing_token: int,
        expected_owner_attempt_id: UUID | None,
        expected_fencing_token: int | None,
        expected_version: int,
        now: datetime | None = None,
        replacement_authorized: bool = False,
        reattach_evidence: ReattachEvidence | None = None,
    ) -> RuntimeExecution:
        if fencing_token <= 0:
            raise InvalidTaskInput("Runtime fencing token must be positive")
        if self.version != expected_version:
            raise InvalidTaskTransition("Runtime execution changed before ownership claim")
        if (
            self.current_owner_attempt_id != expected_owner_attempt_id
            or self.current_fencing_token != expected_fencing_token
        ):
            raise InvalidTaskTransition("Runtime execution owner changed before ownership claim")
        if self.phase.terminal:
            raise InvalidTaskTransition("Terminal Runtime execution cannot be claimed")
        if (
            self.current_owner_attempt_id == attempt_id
            and self.current_fencing_token == fencing_token
        ):
            return self
        if self.current_owner_attempt_id is not None and (
            not replacement_authorized
            or reattach_evidence is None
            or reattach_evidence.execution_id != self.id
            or reattach_evidence.assignment_digest != self.assignment_digest
            or reattach_evidence.provider_execution_ref != self.provider_execution_ref
        ):
            raise InvalidTaskTransition("Active Runtime execution owner cannot be replaced")
        return replace(
            self,
            current_owner_attempt_id=attempt_id,
            current_fencing_token=fencing_token,
            version=self.version + 1,
            updated_at=now or utc_now(),
        )

    def apply_observation(
        self,
        *,
        phase: RuntimeExecutionPhase,
        provider_sequence: int | None,
        provider_execution_ref: str | None = None,
        provider_generation: str | None = None,
        checkpoint_ref: str | None = None,
        workspace_ref: str | None = None,
        now: datetime | None = None,
    ) -> RuntimeExecution:
        allowed: dict[RuntimeExecutionPhase, set[RuntimeExecutionPhase]] = {
            RuntimeExecutionPhase.PREPARED: {
                RuntimeExecutionPhase.DISPATCHING,
                RuntimeExecutionPhase.ACCEPTED,
                RuntimeExecutionPhase.RUNNING,
                RuntimeExecutionPhase.CANCEL_REQUESTED,
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
                RuntimeExecutionPhase.LOST,
                RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            },
            RuntimeExecutionPhase.DISPATCHING: {
                RuntimeExecutionPhase.ACCEPTED,
                RuntimeExecutionPhase.RUNNING,
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
                RuntimeExecutionPhase.LOST,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            },
            RuntimeExecutionPhase.ACCEPTED: {
                RuntimeExecutionPhase.RUNNING,
                RuntimeExecutionPhase.WAITING_INPUT,
                RuntimeExecutionPhase.WAITING_APPROVAL,
                RuntimeExecutionPhase.PAUSE_REQUESTED,
                RuntimeExecutionPhase.CANCEL_REQUESTED,
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
                RuntimeExecutionPhase.LOST,
                RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            },
            RuntimeExecutionPhase.RUNNING: {
                RuntimeExecutionPhase.WAITING_INPUT,
                RuntimeExecutionPhase.WAITING_APPROVAL,
                RuntimeExecutionPhase.PAUSE_REQUESTED,
                RuntimeExecutionPhase.CANCEL_REQUESTED,
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
                RuntimeExecutionPhase.LOST,
                RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            },
            RuntimeExecutionPhase.PAUSE_REQUESTED: {
                RuntimeExecutionPhase.PAUSED,
                RuntimeExecutionPhase.RUNNING,
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
                RuntimeExecutionPhase.LOST,
                RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            },
            RuntimeExecutionPhase.PAUSED: {
                RuntimeExecutionPhase.RUNNING,
                RuntimeExecutionPhase.CANCEL_REQUESTED,
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
                RuntimeExecutionPhase.LOST,
                RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            },
            RuntimeExecutionPhase.CANCEL_REQUESTED: {
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.TIMED_OUT,
                RuntimeExecutionPhase.LOST,
                RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            },
            RuntimeExecutionPhase.WAITING_INPUT: {
                RuntimeExecutionPhase.RUNNING,
                RuntimeExecutionPhase.CANCEL_REQUESTED,
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
                RuntimeExecutionPhase.LOST,
                RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            },
            RuntimeExecutionPhase.WAITING_APPROVAL: {
                RuntimeExecutionPhase.RUNNING,
                RuntimeExecutionPhase.CANCEL_REQUESTED,
                RuntimeExecutionPhase.SUCCEEDED,
                RuntimeExecutionPhase.FAILED,
                RuntimeExecutionPhase.CANCELED,
                RuntimeExecutionPhase.TIMED_OUT,
                RuntimeExecutionPhase.LOST,
                RuntimeExecutionPhase.OUTCOME_UNKNOWN,
            },
        }
        if self.phase.terminal:
            if phase is self.phase:
                return self
            raise InvalidTaskTransition(
                "Conflicting terminal Runtime observations require reconciliation"
            )
        if phase is not self.phase and phase not in allowed.get(self.phase, set()):
            raise InvalidTaskTransition("Runtime observation phase transition is not allowed")
        if provider_sequence is not None and self.provider_sequence is not None:
            if provider_sequence <= self.provider_sequence:
                return self
            if provider_sequence > self.provider_sequence + 1:
                raise InvalidTaskTransition("Runtime observation sequence gap requires inspection")
        for ref in (provider_execution_ref, provider_generation, checkpoint_ref, workspace_ref):
            if ref is not None and (type(ref) is not str or len(ref) > 4096):
                raise InvalidTaskInput("Runtime provider reference is invalid")
        timestamp = now or utc_now()
        return replace(
            self,
            phase=phase,
            provider_sequence=(
                provider_sequence if provider_sequence is not None else self.provider_sequence
            ),
            provider_execution_ref=provider_execution_ref or self.provider_execution_ref,
            provider_generation=provider_generation or self.provider_generation,
            checkpoint_ref=checkpoint_ref or self.checkpoint_ref,
            workspace_ref=workspace_ref or self.workspace_ref,
            version=self.version + 1,
            updated_at=timestamp,
            terminal_at=timestamp if phase.terminal else self.terminal_at,
        )


def _freeze_json(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value
