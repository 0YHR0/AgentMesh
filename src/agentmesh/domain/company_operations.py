from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidCompanyOperation
from agentmesh.domain.tasks import utc_now


class OperationStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


class TriggerKind(str, Enum):
    INTERVAL = "INTERVAL"
    MANUAL = "MANUAL"


class MissedSchedulePolicy(str, Enum):
    SKIP = "SKIP"
    LATEST = "LATEST"
    CATCH_UP_BOUNDED = "CATCH_UP_BOUNDED"
    REQUIRE_REVIEW = "REQUIRE_REVIEW"


class OccurrenceStatus(str, Enum):
    PENDING = "PENDING"
    TASK_CREATED = "TASK_CREATED"
    SKIPPED = "SKIPPED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


def _required(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidCompanyOperation(f"{label} is required")
    if len(normalized) > maximum:
        raise InvalidCompanyOperation(f"{label} must not exceed {maximum} characters")
    return normalized


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise InvalidCompanyOperation(f"{label} must be timezone-aware")
    return value


@dataclass
class CompanyOperation:
    id: UUID
    company_id: UUID
    organization_unit_id: UUID
    initiative_id: UUID | None
    key: str
    name: str
    objective_template: str
    input_template: dict[str, Any]
    trigger_kind: TriggerKind
    trigger_definition: dict[str, Any]
    timezone: str
    missed_policy: MissedSchedulePolicy
    catch_up_limit: int
    concurrency_limit: int
    maximum_runs_per_window: int
    window_seconds: int
    position_bindings: list[UUID]
    tool_capability_allowlist: list[str]
    budget_limit: dict[str, Any]
    approval_policy_id: UUID | None
    status: OperationStatus
    version: int
    content_digest: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        organization_unit_id: UUID,
        key: str,
        name: str,
        objective_template: str,
        input_template: dict[str, Any] | None,
        trigger_kind: TriggerKind,
        trigger_definition: dict[str, Any] | None,
        timezone: str,
        missed_policy: MissedSchedulePolicy,
        catch_up_limit: int = 1,
        concurrency_limit: int = 1,
        maximum_runs_per_window: int = 100,
        window_seconds: int = 86_400,
        position_bindings: list[UUID] | None = None,
        tool_capability_allowlist: list[str] | None = None,
        budget_limit: dict[str, Any] | None = None,
        approval_policy_id: UUID | None = None,
        initiative_id: UUID | None = None,
    ) -> CompanyOperation:
        if not 1 <= catch_up_limit <= 100:
            raise InvalidCompanyOperation("Catch-up limit must be between 1 and 100")
        if not 1 <= concurrency_limit <= 100:
            raise InvalidCompanyOperation("Concurrency limit must be between 1 and 100")
        if maximum_runs_per_window < 1 or window_seconds < 1:
            raise InvalidCompanyOperation("Run window limits must be positive")
        definition = dict(trigger_definition or {})
        if trigger_kind is TriggerKind.INTERVAL:
            seconds = definition.get("interval_seconds")
            if not isinstance(seconds, int) or seconds < 10:
                raise InvalidCompanyOperation(
                    "Interval trigger requires interval_seconds of at least 10"
                )
        elif definition:
            raise InvalidCompanyOperation("Manual trigger does not accept a definition")
        now = utc_now()
        operation = cls(
            id=uuid4(),
            company_id=company_id,
            organization_unit_id=organization_unit_id,
            initiative_id=initiative_id,
            key=_required(key, "Operation key", 63),
            name=_required(name, "Operation name", 160),
            objective_template=_required(
                objective_template, "Operation objective template", 20_000
            ),
            input_template=dict(input_template or {}),
            trigger_kind=trigger_kind,
            trigger_definition=definition,
            timezone=_required(timezone, "Operation timezone", 64),
            missed_policy=missed_policy,
            catch_up_limit=catch_up_limit,
            concurrency_limit=concurrency_limit,
            maximum_runs_per_window=maximum_runs_per_window,
            window_seconds=window_seconds,
            position_bindings=list(position_bindings or []),
            tool_capability_allowlist=list(tool_capability_allowlist or []),
            budget_limit=dict(budget_limit or {}),
            approval_policy_id=approval_policy_id,
            status=OperationStatus.DRAFT,
            version=1,
            content_digest="",
            created_at=now,
            updated_at=now,
        )
        operation.content_digest = operation.calculate_digest()
        return operation

    def calculate_digest(self) -> str:
        content = {
            "company_id": str(self.company_id),
            "organization_unit_id": str(self.organization_unit_id),
            "initiative_id": str(self.initiative_id) if self.initiative_id else None,
            "key": self.key,
            "name": self.name,
            "objective_template": self.objective_template,
            "input_template": self.input_template,
            "trigger_kind": self.trigger_kind.value,
            "trigger_definition": self.trigger_definition,
            "timezone": self.timezone,
            "missed_policy": self.missed_policy.value,
            "catch_up_limit": self.catch_up_limit,
            "concurrency_limit": self.concurrency_limit,
            "maximum_runs_per_window": self.maximum_runs_per_window,
            "window_seconds": self.window_seconds,
            "position_bindings": [str(value) for value in self.position_bindings],
            "tool_capability_allowlist": self.tool_capability_allowlist,
            "budget_limit": self.budget_limit,
            "approval_policy_id": (
                str(self.approval_policy_id) if self.approval_policy_id else None
            ),
        }
        return sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def activate(self) -> None:
        if self.status not in {OperationStatus.DRAFT, OperationStatus.PAUSED}:
            raise InvalidCompanyOperation(f"Cannot activate Operation from {self.status.value}")
        self.status = OperationStatus.ACTIVE
        self.updated_at = utc_now()

    def pause(self) -> None:
        if self.status is not OperationStatus.ACTIVE:
            raise InvalidCompanyOperation(f"Cannot pause Operation from {self.status.value}")
        self.status = OperationStatus.PAUSED
        self.updated_at = utc_now()

    def disable(self) -> None:
        if self.status is OperationStatus.DISABLED:
            raise InvalidCompanyOperation("Operation is already disabled")
        self.status = OperationStatus.DISABLED
        self.updated_at = utc_now()

    def first_due_at(self, activated_at: datetime) -> datetime | None:
        _aware(activated_at, "Activation time")
        if self.trigger_kind is TriggerKind.MANUAL:
            return None
        return activated_at + timedelta(
            seconds=int(self.trigger_definition["interval_seconds"])
        )


@dataclass
class OperationTriggerState:
    operation_id: UUID
    trigger_version: int
    next_due_at: datetime | None
    last_evaluated_at: datetime | None
    last_fired_at: datetime | None
    consecutive_failures: int
    paused_reason: str | None
    fencing_token: int
    updated_at: datetime

    @classmethod
    def create(
        cls, operation: CompanyOperation, *, activated_at: datetime
    ) -> OperationTriggerState:
        return cls(
            operation_id=operation.id,
            trigger_version=operation.version,
            next_due_at=operation.first_due_at(activated_at),
            last_evaluated_at=None,
            last_fired_at=None,
            consecutive_failures=0,
            paused_reason=None,
            fencing_token=0,
            updated_at=activated_at,
        )


@dataclass
class OperationOccurrence:
    id: UUID
    operation_id: UUID
    operation_version: int
    occurrence_key: str
    scheduled_at: datetime
    status: OccurrenceStatus
    task_id: UUID | None
    detail: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls, operation: CompanyOperation, *, occurrence_key: str, scheduled_at: datetime
    ) -> OperationOccurrence:
        now = utc_now()
        return cls(
            id=uuid4(),
            operation_id=operation.id,
            operation_version=operation.version,
            occurrence_key=_required(occurrence_key, "Occurrence key", 512),
            scheduled_at=_aware(scheduled_at, "Occurrence time"),
            status=OccurrenceStatus.PENDING,
            task_id=None,
            detail={},
            created_at=now,
            updated_at=now,
        )


@dataclass
class OperationException:
    id: UUID
    operation_id: UUID
    occurrence_id: UUID | None
    code: str
    message: str
    retryable: bool
    next_retry_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime

    @classmethod
    def capture(
        cls,
        operation_id: UUID,
        *,
        occurrence_id: UUID | None,
        code: str,
        message: str,
        retryable: bool,
        next_retry_at: datetime | None = None,
    ) -> OperationException:
        return cls(
            id=uuid4(),
            operation_id=operation_id,
            occurrence_id=occurrence_id,
            code=_required(code, "Exception code", 63),
            message=_required(message, "Exception message", 4_000),
            retryable=retryable,
            next_retry_at=next_retry_at,
            resolved_at=None,
            created_at=utc_now(),
        )
