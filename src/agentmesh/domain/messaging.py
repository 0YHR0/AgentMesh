from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.tasks import utc_now

RUN_REQUESTED_SCHEMA = "agentmesh.run.requested"
RUN_REQUESTED_VERSION = 1


@dataclass(frozen=True)
class MessageEnvelope:
    schema_name: str
    schema_version: int
    message_id: UUID
    tenant_id: str
    occurred_at: datetime
    producer: str
    correlation_id: UUID
    causation_id: UUID | None
    idempotency_key: str
    payload: dict[str, Any]

    @classmethod
    def run_requested(cls, *, tenant_id: str, task_id: UUID, run_id: UUID) -> MessageEnvelope:
        return cls(
            schema_name=RUN_REQUESTED_SCHEMA,
            schema_version=RUN_REQUESTED_VERSION,
            message_id=uuid4(),
            tenant_id=tenant_id,
            occurred_at=utc_now(),
            producer="agentmesh-control-api",
            correlation_id=task_id,
            causation_id=None,
            idempotency_key=f"run:{run_id}",
            payload={"task_id": str(task_id), "run_id": str(run_id)},
        )

    @classmethod
    def domain_event(
        cls,
        *,
        schema_name: str,
        tenant_id: str,
        aggregate_id: UUID,
        payload: dict[str, Any],
        causation_id: UUID | None = None,
        producer: str = "agentmesh-control-api",
    ) -> MessageEnvelope:
        message_id = uuid4()
        return cls(
            schema_name=schema_name,
            schema_version=1,
            message_id=message_id,
            tenant_id=tenant_id,
            occurred_at=utc_now(),
            producer=producer,
            correlation_id=aggregate_id,
            causation_id=causation_id,
            idempotency_key=f"event:{message_id}",
            payload=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "message_id": str(self.message_id),
            "tenant_id": self.tenant_id,
            "occurred_at": self.occurred_at.isoformat(),
            "producer": self.producer,
            "correlation_id": str(self.correlation_id),
            "causation_id": str(self.causation_id) if self.causation_id else None,
            "idempotency_key": self.idempotency_key,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MessageEnvelope:
        occurred_at = datetime.fromisoformat(str(value["occurred_at"]))
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        return cls(
            schema_name=str(value["schema_name"]),
            schema_version=int(value["schema_version"]),
            message_id=UUID(str(value["message_id"])),
            tenant_id=str(value["tenant_id"]),
            occurred_at=occurred_at,
            producer=str(value["producer"]),
            correlation_id=UUID(str(value["correlation_id"])),
            causation_id=(UUID(str(value["causation_id"])) if value.get("causation_id") else None),
            idempotency_key=str(value["idempotency_key"]),
            payload=dict(value["payload"]),
        )


@dataclass(frozen=True)
class InboxMessage:
    consumer_name: str
    message_id: UUID
    tenant_id: str
    schema_name: str
    schema_version: int
    processed_at: datetime

    @classmethod
    def processed(cls, consumer_name: str, envelope: MessageEnvelope) -> InboxMessage:
        return cls(
            consumer_name=consumer_name,
            message_id=envelope.message_id,
            tenant_id=envelope.tenant_id,
            schema_name=envelope.schema_name,
            schema_version=envelope.schema_version,
            processed_at=utc_now(),
        )


@dataclass(frozen=True)
class IdempotencyRecord:
    scope: str
    key: str
    request_hash: str
    result: dict[str, Any]
    created_at: datetime
    expires_at: datetime

    @classmethod
    def create(
        cls,
        *,
        scope: str,
        key: str,
        request_hash: str,
        result: dict[str, Any],
        ttl: timedelta = timedelta(hours=24),
    ) -> IdempotencyRecord:
        now = utc_now()
        return cls(
            scope=scope,
            key=key,
            request_hash=request_hash,
            result=dict(result),
            created_at=now,
            expires_at=now + ttl,
        )
