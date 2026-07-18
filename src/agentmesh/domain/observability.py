from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.tasks import utc_now


class UsageSource(str, Enum):
    PROVIDER = "PROVIDER"
    ESTIMATED = "ESTIMATED"


@dataclass(frozen=True)
class UsageRecord:
    """Provider/model usage attributed to the Attempt that incurred it.

    Money is stored as integer micros (one millionth of the declared currency)
    so this business ledger never depends on floating-point arithmetic.
    """

    id: UUID
    tenant_id: str
    task_id: UUID
    run_id: UUID
    attempt_id: UUID
    trace_id: str
    provider: str
    model: str
    source: UsageSource
    usage_details: dict[str, int]
    cost_details_micros: dict[str, int]
    currency: str
    pricing_version: str | None
    recorded_at: datetime

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        task_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        trace_id: str,
        provider: str,
        model: str,
        usage_details: dict[str, int],
        cost_details_micros: dict[str, int] | None = None,
        currency: str = "USD",
        source: UsageSource = UsageSource.PROVIDER,
        pricing_version: str | None = None,
        record_id: UUID | None = None,
        recorded_at: datetime | None = None,
    ) -> UsageRecord:
        normalized_tenant = tenant_id.strip()
        normalized_provider = provider.strip()
        normalized_model = model.strip()
        normalized_currency = currency.strip().upper()
        normalized_trace = trace_id.strip().lower()
        if not normalized_tenant or not normalized_provider or not normalized_model:
            raise InvalidTaskInput("Usage tenant, provider, and model must not be empty")
        if len(normalized_tenant) > 128:
            raise InvalidTaskInput("Usage tenant must be at most 128 characters")
        if len(normalized_provider) > 128:
            raise InvalidTaskInput("Usage provider must be at most 128 characters")
        if len(normalized_model) > 255:
            raise InvalidTaskInput("Usage model must be at most 255 characters")
        if len(normalized_trace) != 32 or any(
            character not in "0123456789abcdef" for character in normalized_trace
        ):
            raise InvalidTaskInput("Usage trace ID must be 32 lowercase hexadecimal characters")
        if normalized_trace == "0" * 32:
            raise InvalidTaskInput("Usage trace ID must not be all zeroes")
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise InvalidTaskInput("Usage currency must be a three-letter code")

        normalized_usage = cls._normalize_details("usage", usage_details, required=True)
        normalized_cost = cls._normalize_details(
            "cost",
            cost_details_micros or {},
            required=False,
        )
        normalized_pricing_version = (pricing_version or "").strip() or None
        if normalized_pricing_version and len(normalized_pricing_version) > 128:
            raise InvalidTaskInput("Usage pricing version must be at most 128 characters")
        timestamp = recorded_at or utc_now()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise InvalidTaskInput("Usage recorded_at must include a timezone")
        try:
            normalized_source = UsageSource(source)
        except ValueError as exc:
            raise InvalidTaskInput("Usage source must be PROVIDER or ESTIMATED") from exc
        return cls(
            id=record_id or uuid4(),
            tenant_id=normalized_tenant,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            trace_id=normalized_trace,
            provider=normalized_provider,
            model=normalized_model,
            source=normalized_source,
            usage_details=normalized_usage,
            cost_details_micros=normalized_cost,
            currency=normalized_currency,
            pricing_version=normalized_pricing_version,
            recorded_at=timestamp.astimezone(timezone.utc),
        )

    @staticmethod
    def _normalize_details(
        label: str,
        details: dict[str, int],
        *,
        required: bool,
    ) -> dict[str, int]:
        if required and not details:
            raise InvalidTaskInput("Usage details must not be empty")
        if len(details) > 64:
            raise InvalidTaskInput(f"Usage {label} details must contain at most 64 buckets")
        normalized: dict[str, int] = {}
        for raw_key, value in details.items():
            key = raw_key.strip()
            if not key:
                raise InvalidTaskInput(f"Usage {label} detail names must not be empty")
            if len(key) > 128:
                raise InvalidTaskInput(f"Usage {label} detail names must be at most 128 characters")
            if key in normalized:
                raise InvalidTaskInput(
                    f"Usage {label} detail name '{key}' is duplicated after normalization"
                )
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 9_223_372_036_854_775_807
            ):
                raise InvalidTaskInput(
                    f"Usage {label} detail '{key}' must be a non-negative 64-bit integer"
                )
            normalized[key] = value
        return normalized

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "tenant_id": self.tenant_id,
            "task_id": str(self.task_id),
            "run_id": str(self.run_id),
            "attempt_id": str(self.attempt_id),
            "trace_id": self.trace_id,
            "provider": self.provider,
            "model": self.model,
            "source": self.source.value,
            "usage_details": dict(self.usage_details),
            "cost_details_micros": dict(self.cost_details_micros),
            "currency": self.currency,
            "pricing_version": self.pricing_version,
            "recorded_at": self.recorded_at.isoformat(),
        }

    @classmethod
    def from_checkpoint(cls, value: dict[str, Any]) -> UsageRecord:
        return cls.create(
            record_id=UUID(str(value["id"])),
            tenant_id=str(value["tenant_id"]),
            task_id=UUID(str(value["task_id"])),
            run_id=UUID(str(value["run_id"])),
            attempt_id=UUID(str(value["attempt_id"])),
            trace_id=str(value["trace_id"]),
            provider=str(value["provider"]),
            model=str(value["model"]),
            source=UsageSource(str(value["source"])),
            usage_details=dict(value["usage_details"]),
            cost_details_micros=dict(value["cost_details_micros"]),
            currency=str(value["currency"]),
            pricing_version=(
                str(value["pricing_version"]) if value.get("pricing_version") else None
            ),
            recorded_at=datetime.fromisoformat(str(value["recorded_at"])),
        )


@dataclass(frozen=True)
class TaskUsage:
    task_id: UUID
    usage_details: dict[str, int]
    cost_details_micros_by_currency: dict[str, dict[str, int]]
    records: list[UsageRecord]

    @classmethod
    def summarize(cls, task_id: UUID, records: list[UsageRecord]) -> TaskUsage:
        usage: dict[str, int] = {}
        costs: dict[str, dict[str, int]] = {}
        for record in records:
            for key, value in record.usage_details.items():
                usage[key] = usage.get(key, 0) + value
            currency_costs = costs.setdefault(record.currency, {})
            for key, value in record.cost_details_micros.items():
                currency_costs[key] = currency_costs.get(key, 0) + value
        return cls(
            task_id=task_id,
            usage_details=usage,
            cost_details_micros_by_currency=costs,
            records=list(records),
        )
