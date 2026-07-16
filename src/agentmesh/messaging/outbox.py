from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from redis import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.infrastructure.postgres.models import OutboxEventRecord


@dataclass(frozen=True)
class ClaimedOutboxEvent:
    id: UUID
    envelope: MessageEnvelope


class SqlAlchemyOutboxStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def claim_batch(
        self,
        *,
        relay_id: str,
        batch_size: int,
        claim_duration: timedelta,
    ) -> list[ClaimedOutboxEvent]:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            statement = (
                select(OutboxEventRecord)
                .where(
                    OutboxEventRecord.status == "PENDING",
                    OutboxEventRecord.available_at <= now,
                    or_(
                        OutboxEventRecord.claimed_until.is_(None),
                        OutboxEventRecord.claimed_until <= now,
                    ),
                )
                .order_by(OutboxEventRecord.created_at.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            records = list(session.scalars(statement))
            for record in records:
                record.claimed_by = relay_id
                record.claimed_until = now + claim_duration
                record.attempt_count += 1
            return [
                ClaimedOutboxEvent(
                    id=record.id,
                    envelope=MessageEnvelope.from_dict(dict(record.envelope)),
                )
                for record in records
            ]

    def mark_published(self, event_id: UUID, *, relay_id: str) -> None:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            session.execute(
                update(OutboxEventRecord)
                .where(
                    OutboxEventRecord.id == event_id,
                    OutboxEventRecord.claimed_by == relay_id,
                    OutboxEventRecord.status == "PENDING",
                )
                .values(
                    status="PUBLISHED",
                    published_at=now,
                    claimed_by=None,
                    claimed_until=None,
                    last_error=None,
                )
            )

    def mark_failed(
        self,
        event_id: UUID,
        *,
        relay_id: str,
        error: str,
        retry_delay: timedelta,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            session.execute(
                update(OutboxEventRecord)
                .where(
                    OutboxEventRecord.id == event_id,
                    OutboxEventRecord.claimed_by == relay_id,
                    OutboxEventRecord.status == "PENDING",
                )
                .values(
                    available_at=now + retry_delay,
                    claimed_by=None,
                    claimed_until=None,
                    last_error=error[:2_000],
                )
            )


class RedisStreamPublisher:
    def __init__(self, redis_client: Redis, stream_name: str) -> None:
        self._redis = redis_client
        self._stream_name = stream_name

    def publish(self, envelope: MessageEnvelope) -> str:
        return str(
            self._redis.xadd(
                self._stream_name,
                {"envelope": json.dumps(envelope.to_dict(), separators=(",", ":"))},
            )
        )


class OutboxRelay:
    def __init__(
        self,
        *,
        relay_id: str,
        store: SqlAlchemyOutboxStore,
        publisher: RedisStreamPublisher,
        batch_size: int,
        claim_duration: timedelta,
        retry_delay: timedelta,
    ) -> None:
        self._relay_id = relay_id
        self._store = store
        self._publisher = publisher
        self._batch_size = batch_size
        self._claim_duration = claim_duration
        self._retry_delay = retry_delay

    def publish_once(self) -> int:
        claimed = self._store.claim_batch(
            relay_id=self._relay_id,
            batch_size=self._batch_size,
            claim_duration=self._claim_duration,
        )
        published = 0
        for event in claimed:
            try:
                self._publisher.publish(event.envelope)
            except Exception as exc:
                self._store.mark_failed(
                    event.id,
                    relay_id=self._relay_id,
                    error=f"{type(exc).__name__}: {exc}",
                    retry_delay=self._retry_delay,
                )
                continue
            self._store.mark_published(event.id, relay_id=self._relay_id)
            published += 1
        return published
