from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidCompanyGoal
from agentmesh.domain.tasks import utc_now


class OperatingCycleStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REVIEWING = "REVIEWING"
    CLOSED = "CLOSED"


class ObjectiveStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    ACHIEVED = "ACHIEVED"
    CANCELED = "CANCELED"


class KeyResultStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ACHIEVED = "ACHIEVED"
    MISSED = "MISSED"
    CANCELED = "CANCELED"


class InitiativeStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


def _required(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidCompanyGoal(f"{label} is required")
    if len(normalized) > maximum:
        raise InvalidCompanyGoal(f"{label} must not exceed {maximum} characters")
    return normalized


def _decimal(value: str | int | float | Decimal, label: str) -> str:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise InvalidCompanyGoal(f"{label} must be numeric") from exc
    if not parsed.is_finite():
        raise InvalidCompanyGoal(f"{label} must be finite")
    return format(parsed.normalize(), "f")


@dataclass
class OperatingCycle:
    id: UUID
    company_id: UUID
    name: str
    starts_at: datetime
    ends_at: datetime
    status: OperatingCycleStatus
    approved_by: str | None
    approved_at: datetime | None
    review_schedule: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        name: str,
        starts_at: datetime,
        ends_at: datetime,
        review_schedule: dict[str, Any] | None = None,
    ) -> OperatingCycle:
        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            raise InvalidCompanyGoal("Operating Cycle timestamps must be timezone-aware")
        if ends_at <= starts_at:
            raise InvalidCompanyGoal("Operating Cycle end must be after its start")
        now = utc_now()
        return cls(
            id=uuid4(),
            company_id=company_id,
            name=_required(name, "Operating Cycle name", 160),
            starts_at=starts_at,
            ends_at=ends_at,
            status=OperatingCycleStatus.DRAFT,
            approved_by=None,
            approved_at=None,
            review_schedule=dict(review_schedule or {}),
            version=1,
            created_at=now,
            updated_at=now,
        )

    def approve(self, actor: str) -> None:
        self._require(OperatingCycleStatus.DRAFT, "approve")
        now = utc_now()
        self.status = OperatingCycleStatus.APPROVED
        self.approved_by = _required(actor, "Approving principal", 128)
        self.approved_at = now
        self._touch(now)

    def activate(self) -> None:
        if self.status not in {OperatingCycleStatus.APPROVED, OperatingCycleStatus.PAUSED}:
            raise InvalidCompanyGoal(f"Cannot activate Operating Cycle from {self.status.value}")
        self.status = OperatingCycleStatus.ACTIVE
        self._touch()

    def pause(self) -> None:
        self._require(OperatingCycleStatus.ACTIVE, "pause")
        self.status = OperatingCycleStatus.PAUSED
        self._touch()

    def review(self) -> None:
        if self.status not in {OperatingCycleStatus.ACTIVE, OperatingCycleStatus.PAUSED}:
            raise InvalidCompanyGoal(f"Cannot review Operating Cycle from {self.status.value}")
        self.status = OperatingCycleStatus.REVIEWING
        self._touch()

    def close(self) -> None:
        self._require(OperatingCycleStatus.REVIEWING, "close")
        self.status = OperatingCycleStatus.CLOSED
        self._touch()

    def _require(self, expected: OperatingCycleStatus, action: str) -> None:
        if self.status is not expected:
            raise InvalidCompanyGoal(
                f"Cannot {action} Operating Cycle from {self.status.value}"
            )

    def _touch(self, now: datetime | None = None) -> None:
        self.version += 1
        self.updated_at = now or utc_now()


@dataclass
class CompanyObjective:
    id: UUID
    company_id: UUID
    cycle_id: UUID
    owner_position_id: UUID
    statement: str
    rationale: str
    status: ObjectiveStatus
    priority: int
    target_date: datetime
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        cycle_id: UUID,
        owner_position_id: UUID,
        statement: str,
        rationale: str,
        priority: int,
        target_date: datetime,
    ) -> CompanyObjective:
        if priority < 1 or priority > 5:
            raise InvalidCompanyGoal("Objective priority must be between 1 and 5")
        if target_date.tzinfo is None:
            raise InvalidCompanyGoal("Objective target date must be timezone-aware")
        now = utc_now()
        return cls(
            id=uuid4(),
            company_id=company_id,
            cycle_id=cycle_id,
            owner_position_id=owner_position_id,
            statement=_required(statement, "Objective statement", 2_000),
            rationale=_required(rationale, "Objective rationale", 10_000),
            status=ObjectiveStatus.PROPOSED,
            priority=priority,
            target_date=target_date,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def approve(self) -> None:
        self._transition(ObjectiveStatus.PROPOSED, ObjectiveStatus.APPROVED)

    def activate(self) -> None:
        self._transition(ObjectiveStatus.APPROVED, ObjectiveStatus.ACTIVE)

    def achieve(self) -> None:
        self._transition(ObjectiveStatus.ACTIVE, ObjectiveStatus.ACHIEVED)

    def _transition(self, expected: ObjectiveStatus, target: ObjectiveStatus) -> None:
        if self.status is not expected:
            raise InvalidCompanyGoal(
                f"Cannot move Objective from {self.status.value} to {target.value}"
            )
        self.status = target
        self.version += 1
        self.updated_at = utc_now()


@dataclass
class KeyResult:
    id: UUID
    company_id: UUID
    objective_id: UUID
    metric_key: str
    unit: str
    baseline: str
    target: str
    current_verified_value: str | None
    current_estimated_value: str | None
    measurement_source: str
    status: KeyResultStatus
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        objective_id: UUID,
        metric_key: str,
        unit: str,
        baseline: str,
        target: str,
        measurement_source: str,
    ) -> KeyResult:
        now = utc_now()
        return cls(
            id=uuid4(),
            company_id=company_id,
            objective_id=objective_id,
            metric_key=_required(metric_key, "Key Result metric key", 128),
            unit=_required(unit, "Key Result unit", 32),
            baseline=_decimal(baseline, "Key Result baseline"),
            target=_decimal(target, "Key Result target"),
            current_verified_value=None,
            current_estimated_value=None,
            measurement_source=_required(
                measurement_source, "Key Result measurement source", 255
            ),
            status=KeyResultStatus.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def record_estimate(self, value: str) -> None:
        self.current_estimated_value = _decimal(value, "Estimated value")
        self._touch()

    def record_verified(self, value: str, source: str) -> None:
        self.current_verified_value = _decimal(value, "Verified value")
        self.measurement_source = _required(source, "Measurement source", 255)
        self._touch()

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = utc_now()


@dataclass
class Initiative:
    id: UUID
    company_id: UUID
    objective_id: UUID
    owner_unit_id: UUID
    title: str
    outcome_contract: dict[str, Any]
    budget_allocation_id: UUID | None
    status: InitiativeStatus
    starts_at: datetime
    ends_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        objective_id: UUID,
        owner_unit_id: UUID,
        title: str,
        outcome_contract: dict[str, Any],
        budget_allocation_id: UUID | None = None,
    ) -> Initiative:
        contract = dict(outcome_contract)
        if not contract:
            raise InvalidCompanyGoal("Initiative outcome contract is required")
        now = utc_now()
        return cls(
            id=uuid4(),
            company_id=company_id,
            objective_id=objective_id,
            owner_unit_id=owner_unit_id,
            title=_required(title, "Initiative title", 240),
            outcome_contract=contract,
            budget_allocation_id=budget_allocation_id,
            status=InitiativeStatus.PROPOSED,
            starts_at=now,
            ends_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def approve(self) -> None:
        self._transition(InitiativeStatus.PROPOSED, InitiativeStatus.APPROVED)

    def activate(self) -> None:
        self._transition(InitiativeStatus.APPROVED, InitiativeStatus.ACTIVE)

    def complete(self) -> None:
        self._transition(InitiativeStatus.ACTIVE, InitiativeStatus.COMPLETED)
        self.ends_at = self.updated_at

    def _transition(self, expected: InitiativeStatus, target: InitiativeStatus) -> None:
        if self.status is not expected:
            raise InvalidCompanyGoal(
                f"Cannot move Initiative from {self.status.value} to {target.value}"
            )
        self.status = target
        self.version += 1
        self.updated_at = utc_now()


@dataclass(frozen=True)
class InitiativeTaskLink:
    initiative_id: UUID
    task_id: UUID
    created_by: str
    created_at: datetime
