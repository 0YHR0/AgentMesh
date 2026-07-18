from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

from agentmesh.domain.errors import InvalidTaskInput


class BudgetSettlementSource(str, Enum):
    ACTUAL = "ACTUAL"
    CONSERVATIVE_ESTIMATE = "CONSERVATIVE_ESTIMATE"
    RELEASED = "RELEASED"


@dataclass(frozen=True)
class TaskBudget:
    """Immutable limits and conservative per-Attempt reservation sizes."""

    max_runs: int | None = None
    max_attempts: int | None = None
    max_tokens: int | None = None
    token_reservation_per_attempt: int = 0
    max_cost_micros: int | None = None
    cost_reservation_micros_per_attempt: int = 0
    currency: str = "USD"
    deadline: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        max_runs: int | None = None,
        max_attempts: int | None = None,
        max_tokens: int | None = None,
        token_reservation_per_attempt: int = 0,
        max_cost_micros: int | None = None,
        cost_reservation_micros_per_attempt: int = 0,
        currency: str = "USD",
        deadline: datetime | None = None,
    ) -> TaskBudget:
        values = (max_runs, max_attempts, max_tokens, max_cost_micros)
        if any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 < value <= 9_223_372_036_854_775_807
            )
            for value in values
        ):
            raise InvalidTaskInput("Budget limits must be positive 64-bit integers")
        reservations = (
            token_reservation_per_attempt,
            cost_reservation_micros_per_attempt,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 9_223_372_036_854_775_807
            for value in reservations
        ):
            raise InvalidTaskInput("Budget reservations must be non-negative 64-bit integers")
        if max_tokens is None and token_reservation_per_attempt:
            raise InvalidTaskInput("Token reservation requires a Token budget")
        if max_tokens is not None and not 0 < token_reservation_per_attempt <= max_tokens:
            raise InvalidTaskInput(
                "Token budget requires a positive per-Attempt reservation within the limit"
            )
        if max_cost_micros is None and cost_reservation_micros_per_attempt:
            raise InvalidTaskInput("Cost reservation requires a cost budget")
        if max_cost_micros is not None and not (
            0 < cost_reservation_micros_per_attempt <= max_cost_micros
        ):
            raise InvalidTaskInput(
                "Cost budget requires a positive per-Attempt reservation within the limit"
            )
        normalized_currency = currency.strip().upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise InvalidTaskInput("Budget currency must be a three-letter code")
        if deadline is not None:
            if deadline.tzinfo is None or deadline.utcoffset() is None:
                raise InvalidTaskInput("Budget deadline must include a timezone")
            deadline = deadline.astimezone(timezone.utc)
        if all(value is None for value in values) and deadline is None:
            raise InvalidTaskInput("Budget must define at least one limit or deadline")
        return cls(
            max_runs=max_runs,
            max_attempts=max_attempts,
            max_tokens=max_tokens,
            token_reservation_per_attempt=token_reservation_per_attempt,
            max_cost_micros=max_cost_micros,
            cost_reservation_micros_per_attempt=cost_reservation_micros_per_attempt,
            currency=normalized_currency,
            deadline=deadline,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "max_runs": self.max_runs,
            "max_attempts": self.max_attempts,
            "max_tokens": self.max_tokens,
            "token_reservation_per_attempt": self.token_reservation_per_attempt,
            "max_cost_micros": self.max_cost_micros,
            "cost_reservation_micros_per_attempt": self.cost_reservation_micros_per_attempt,
            "currency": self.currency,
            "deadline": self.deadline.isoformat() if self.deadline else None,
        }

    def require_monotonic_increase(self, replacement: TaskBudget) -> None:
        if replacement.currency != self.currency:
            raise InvalidTaskInput("Budget increase cannot change currency")
        if (
            replacement.token_reservation_per_attempt != self.token_reservation_per_attempt
            or replacement.cost_reservation_micros_per_attempt
            != self.cost_reservation_micros_per_attempt
        ):
            raise InvalidTaskInput("Budget increase cannot change reservation sizes")
        changed = False
        for label in ("max_runs", "max_attempts", "max_tokens", "max_cost_micros"):
            current = getattr(self, label)
            proposed = getattr(replacement, label)
            if current is None:
                if proposed is not None:
                    raise InvalidTaskInput(f"Budget increase cannot constrain unlimited {label}")
            elif proposed is None or proposed < current:
                raise InvalidTaskInput(f"Budget increase cannot reduce {label}")
            elif proposed > current:
                changed = True
        if self.deadline is None:
            if replacement.deadline is not None:
                raise InvalidTaskInput("Budget increase cannot add a deadline")
        elif replacement.deadline is None or replacement.deadline < self.deadline:
            raise InvalidTaskInput("Budget increase cannot shorten the deadline")
        elif replacement.deadline > self.deadline:
            changed = True
        if not changed:
            raise InvalidTaskInput("Replacement budget must increase at least one limit")

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> TaskBudget:
        raw_deadline = value.get("deadline")
        return cls.create(
            max_runs=value.get("max_runs"),  # type: ignore[arg-type]
            max_attempts=value.get("max_attempts"),  # type: ignore[arg-type]
            max_tokens=value.get("max_tokens"),  # type: ignore[arg-type]
            token_reservation_per_attempt=int(value.get("token_reservation_per_attempt", 0)),
            max_cost_micros=value.get("max_cost_micros"),  # type: ignore[arg-type]
            cost_reservation_micros_per_attempt=int(
                value.get("cost_reservation_micros_per_attempt", 0)
            ),
            currency=str(value.get("currency", "USD")),
            deadline=(datetime.fromisoformat(str(raw_deadline)) if raw_deadline else None),
        )


@dataclass(frozen=True)
class TaskBudgetStatus:
    task_id: UUID
    policy: TaskBudget
    run_count: int
    attempt_count: int
    settled_tokens: int
    reserved_tokens: int
    settled_cost_micros: int
    reserved_cost_micros: int
    exhausted_reason: str | None
