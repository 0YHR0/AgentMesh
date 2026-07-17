from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from redis import Redis
from redis.exceptions import ResponseError
from sqlalchemy import delete, exists, func, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.infrastructure.postgres.models import InboxMessageRecord, OutboxEventRecord

MESSAGING_RETENTION_LOCK_ID = 0x4147454E544D4553


@dataclass(frozen=True)
class DatabaseRetentionMetrics:
    outbox_deleted: int
    inbox_deleted: int
    outbox_rows: int
    outbox_pending_rows: int
    outbox_published_rows: int
    outbox_quarantined_rows: int
    inbox_rows: int
    inbox_cleanup_blocked: bool
    inbox_guard_scan_complete: bool
    inbox_guard_scanned_entries: int
    inbox_guard_protected_messages: int
    oldest_unsettled_delivery_age_seconds: float | None
    oldest_outbox_pending_age_seconds: float | None
    oldest_outbox_published_age_seconds: float | None
    oldest_outbox_quarantined_age_seconds: float | None
    oldest_inbox_age_seconds: float | None


@dataclass(frozen=True)
class StreamRetentionMetrics:
    stream_name: str
    deleted_entries: int
    length: int
    max_entries: int
    over_capacity_entries: int
    consumer_groups: int
    pending_entries: int
    oldest_entry_age_seconds: float | None
    oldest_pending_age_seconds: float | None
    safe_group_floor_id: str | None
    skipped_reason: str | None


@dataclass(frozen=True)
class UnsettledDeliveryInspection:
    oldest_stream_id: str | None
    oldest_occurred_at: datetime | None
    message_keys: tuple[tuple[str, UUID], ...]
    scanned_entries: int
    complete: bool


@dataclass(frozen=True)
class RetentionReport:
    observed_at: datetime
    database: DatabaseRetentionMetrics
    streams: tuple[StreamRetentionMetrics, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["observed_at"] = self.observed_at.isoformat()
        return payload


@dataclass(frozen=True)
class StreamRetentionPolicy:
    stream_name: str
    retention: timedelta
    max_entries: int
    required_group: str | None = None
    protects_inbox: bool = False


@dataclass(frozen=True)
class MessagingRetentionPolicy:
    outbox_retention: timedelta
    inbox_retention: timedelta
    batch_size: int
    streams: tuple[StreamRetentionPolicy, ...]


class SqlAlchemyMessageRetentionStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def prune_outbox(self, *, cutoff: datetime, batch_size: int) -> int:
        candidates = (
            select(OutboxEventRecord.id)
            .where(
                OutboxEventRecord.status == "PUBLISHED",
                OutboxEventRecord.published_at.is_not(None),
                OutboxEventRecord.published_at < cutoff,
            )
            .order_by(OutboxEventRecord.published_at.asc(), OutboxEventRecord.id.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
            .cte("expired_outbox_events")
        )
        statement = (
            delete(OutboxEventRecord)
            .where(OutboxEventRecord.id.in_(select(candidates.c.id)))
            .returning(OutboxEventRecord.id)
            .execution_options(synchronize_session=False)
        )
        with self._session_factory() as session, session.begin():
            return len(list(session.scalars(statement)))

    def prune_inbox(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
        protected_message_keys: Sequence[tuple[str, UUID]],
    ) -> int:
        eligibility = [
            InboxMessageRecord.processed_at < cutoff,
            ~exists(
                select(OutboxEventRecord.id).where(
                    OutboxEventRecord.tenant_id == InboxMessageRecord.tenant_id,
                    OutboxEventRecord.id == InboxMessageRecord.message_id,
                )
            ),
        ]
        if protected_message_keys:
            eligibility.append(
                tuple_(
                    InboxMessageRecord.tenant_id,
                    InboxMessageRecord.message_id,
                ).not_in(protected_message_keys)
            )
        candidates = (
            select(
                InboxMessageRecord.tenant_id,
                InboxMessageRecord.consumer_name,
                InboxMessageRecord.message_id,
            )
            .where(*eligibility)
            .order_by(
                InboxMessageRecord.processed_at.asc(),
                InboxMessageRecord.tenant_id.asc(),
                InboxMessageRecord.consumer_name.asc(),
                InboxMessageRecord.message_id.asc(),
            )
            .limit(batch_size)
            .with_for_update(skip_locked=True)
            .cte("expired_inbox_messages")
        )
        candidate_keys = select(
            candidates.c.tenant_id,
            candidates.c.consumer_name,
            candidates.c.message_id,
        )
        statement = (
            delete(InboxMessageRecord)
            .where(
                tuple_(
                    InboxMessageRecord.tenant_id,
                    InboxMessageRecord.consumer_name,
                    InboxMessageRecord.message_id,
                ).in_(candidate_keys)
            )
            .returning(InboxMessageRecord.message_id)
            .execution_options(synchronize_session=False)
        )
        with self._session_factory() as session, session.begin():
            return len(list(session.scalars(statement)))

    @contextmanager
    def try_cycle_lock(self) -> Iterator[bool]:
        with self._session_factory() as session:
            acquired = bool(
                session.scalar(select(func.pg_try_advisory_lock(MESSAGING_RETENTION_LOCK_ID)))
            )
            try:
                yield acquired
            finally:
                if acquired:
                    session.scalar(select(func.pg_advisory_unlock(MESSAGING_RETENTION_LOCK_ID)))

    def measure(
        self,
        *,
        now: datetime,
        outbox_deleted: int,
        inbox_deleted: int,
        inbox_cleanup_blocked: bool,
        inbox_guard_scan_complete: bool,
        inbox_guard_scanned_entries: int,
        inbox_guard_protected_messages: int,
        oldest_unsettled_delivery_age_seconds: float | None,
    ) -> DatabaseRetentionMetrics:
        with self._session_factory() as session:
            outbox = session.execute(
                select(
                    func.count(OutboxEventRecord.id).label("rows"),
                    func.count(OutboxEventRecord.id)
                    .filter(OutboxEventRecord.status == "PENDING")
                    .label("pending_rows"),
                    func.count(OutboxEventRecord.id)
                    .filter(OutboxEventRecord.status == "PUBLISHED")
                    .label("published_rows"),
                    func.count(OutboxEventRecord.id)
                    .filter(OutboxEventRecord.status == "QUARANTINED")
                    .label("quarantined_rows"),
                    func.min(OutboxEventRecord.created_at)
                    .filter(OutboxEventRecord.status == "PENDING")
                    .label("oldest_pending"),
                    func.min(OutboxEventRecord.published_at)
                    .filter(OutboxEventRecord.status == "PUBLISHED")
                    .label("oldest_published"),
                    func.min(OutboxEventRecord.quarantined_at)
                    .filter(OutboxEventRecord.status == "QUARANTINED")
                    .label("oldest_quarantined"),
                )
            ).one()
            inbox = session.execute(
                select(
                    func.count(InboxMessageRecord.message_id).label("rows"),
                    func.min(InboxMessageRecord.processed_at).label("oldest"),
                )
            ).one()

        outbox_values = outbox._mapping
        inbox_values = inbox._mapping
        return DatabaseRetentionMetrics(
            outbox_deleted=outbox_deleted,
            inbox_deleted=inbox_deleted,
            outbox_rows=int(outbox_values["rows"]),
            outbox_pending_rows=int(outbox_values["pending_rows"]),
            outbox_published_rows=int(outbox_values["published_rows"]),
            outbox_quarantined_rows=int(outbox_values["quarantined_rows"]),
            inbox_rows=int(inbox_values["rows"]),
            inbox_cleanup_blocked=inbox_cleanup_blocked,
            inbox_guard_scan_complete=inbox_guard_scan_complete,
            inbox_guard_scanned_entries=inbox_guard_scanned_entries,
            inbox_guard_protected_messages=inbox_guard_protected_messages,
            oldest_unsettled_delivery_age_seconds=(oldest_unsettled_delivery_age_seconds),
            oldest_outbox_pending_age_seconds=_age_seconds(now, outbox_values["oldest_pending"]),
            oldest_outbox_published_age_seconds=_age_seconds(
                now, outbox_values["oldest_published"]
            ),
            oldest_outbox_quarantined_age_seconds=_age_seconds(
                now, outbox_values["oldest_quarantined"]
            ),
            oldest_inbox_age_seconds=_age_seconds(now, inbox_values["oldest"]),
        )


class RedisStreamRetentionStore:
    def __init__(self, redis_client: Redis) -> None:
        self._redis = redis_client

    def prune(
        self,
        *,
        policy: StreamRetentionPolicy,
        now: datetime,
        batch_size: int,
    ) -> StreamRetentionMetrics:
        length_before = int(self._redis.xlen(policy.stream_name))
        groups = self._groups(policy.stream_name)
        group_names = {_as_text(_mapping_value(group, "name")) for group in groups}
        pending_entries = sum(int(_mapping_value(group, "pending", 0)) for group in groups)
        oldest_pending_id: str | None = None

        if policy.required_group is not None and policy.required_group not in group_names:
            return self._measure_stream(
                policy=policy,
                now=now,
                deleted_entries=0,
                groups=groups,
                pending_entries=pending_entries,
                oldest_pending_id=None,
                safe_group_floor_id=None,
                skipped_reason="required_consumer_group_missing",
            )

        safe_group_floor_id: str | None = None
        for group in groups:
            group_name = _as_text(_mapping_value(group, "name"))
            group_pending = int(_mapping_value(group, "pending", 0))
            last_delivered_id = _as_text(_mapping_value(group, "last-delivered-id", "0-0"))
            if group_pending > 0:
                pending = self._redis.xpending_range(
                    policy.stream_name,
                    group_name,
                    min="-",
                    max="+",
                    count=1,
                )
                if not pending:
                    return self._measure_stream(
                        policy=policy,
                        now=now,
                        deleted_entries=0,
                        groups=groups,
                        pending_entries=pending_entries,
                        oldest_pending_id=oldest_pending_id,
                        safe_group_floor_id=safe_group_floor_id,
                        skipped_reason="pending_metadata_unavailable",
                    )
                earliest_pending_id = _as_text(_mapping_value(pending[0], "message_id"))
                oldest_pending_id = _minimum_stream_id(oldest_pending_id, earliest_pending_id)
                group_floor_id = _minimum_stream_id(last_delivered_id, earliest_pending_id) or "0-0"
            else:
                group_floor_id = last_delivered_id
            safe_group_floor_id = _minimum_stream_id(safe_group_floor_id, group_floor_id)

        retention_cutoff_id = _datetime_stream_id(now - policy.retention)
        time_boundary = retention_cutoff_id
        if safe_group_floor_id is not None:
            time_boundary = _minimum_stream_id(time_boundary, safe_group_floor_id) or "0-0"

        time_candidates = self._candidate_ids(
            policy.stream_name,
            before_id=time_boundary,
            count=batch_size,
        )
        excess = max(0, length_before - policy.max_entries)
        capacity_candidates = self._candidate_ids(
            policy.stream_name,
            before_id=safe_group_floor_id or "+",
            count=min(batch_size, excess),
        )
        candidate_ids = sorted(
            set(time_candidates).union(capacity_candidates),
            key=_stream_id_parts,
        )[:batch_size]
        deleted_entries = 0
        if candidate_ids:
            deleted_entries = int(self._redis.xdel(policy.stream_name, *candidate_ids))

        return self._measure_stream(
            policy=policy,
            now=now,
            deleted_entries=deleted_entries,
            groups=groups,
            pending_entries=pending_entries,
            oldest_pending_id=oldest_pending_id,
            safe_group_floor_id=safe_group_floor_id,
            skipped_reason=None,
        )

    def inspect_unsettled(
        self,
        policy: StreamRetentionPolicy,
        *,
        scan_limit: int,
    ) -> UnsettledDeliveryInspection:
        if scan_limit <= 0:
            raise ValueError("scan_limit must be positive")

        groups_before = self._groups(policy.stream_name)
        group_names = {_as_text(_mapping_value(group, "name")) for group in groups_before}
        if not groups_before or (
            policy.required_group is not None and policy.required_group not in group_names
        ):
            entries = self._redis.xrange(
                policy.stream_name,
                min="-",
                max="+",
                count=scan_limit + 1,
            )
            selected = entries[:scan_limit]
            complete = len(entries) <= scan_limit
            complete = complete and self._groups_unchanged(policy.stream_name, groups_before)
            return self._inspect_entries(selected, complete=complete)

        if len(groups_before) > scan_limit:
            return UnsettledDeliveryInspection(
                oldest_stream_id=None,
                oldest_occurred_at=None,
                message_keys=(),
                scanned_entries=0,
                complete=False,
            )

        selected_by_id: dict[str, tuple[Any, Mapping[Any, Any]]] = {}
        references_examined = 0
        complete = True
        for group in groups_before:
            group_name = _as_text(_mapping_value(group, "name"))
            pending_count = int(_mapping_value(group, "pending", 0))
            remaining = scan_limit - references_examined
            if pending_count > 0:
                if remaining == 0:
                    complete = False
                    break
                pending = self._redis.xpending_range(
                    policy.stream_name,
                    group_name,
                    min="-",
                    max="+",
                    count=min(pending_count, remaining + 1),
                )
                if len(pending) > remaining:
                    pending = pending[:remaining]
                    complete = False
                elif len(pending) != pending_count:
                    complete = False
                references_examined += len(pending)
                for metadata in pending:
                    message_id = _as_text(_mapping_value(metadata, "message_id"))
                    entry = self._redis.xrange(
                        policy.stream_name,
                        min=message_id,
                        max=message_id,
                        count=1,
                    )
                    if entry:
                        selected_by_id[message_id] = entry[0]
                if not complete:
                    break

            remaining = scan_limit - references_examined
            last_delivered_id = _as_text(_mapping_value(group, "last-delivered-id", "0-0"))
            unread = self._redis.xrange(
                policy.stream_name,
                min=f"({last_delivered_id}",
                max="+",
                count=remaining + 1,
            )
            if len(unread) > remaining:
                unread = unread[:remaining]
                complete = False
            references_examined += len(unread)
            for entry in unread:
                selected_by_id[_as_text(entry[0])] = entry
            if not complete:
                break

        complete = complete and self._groups_unchanged(policy.stream_name, groups_before)
        return self._inspect_entries(
            sorted(selected_by_id.values(), key=lambda entry: _stream_id_parts(_as_text(entry[0]))),
            complete=complete,
        )

    def _groups_unchanged(
        self,
        stream_name: str,
        groups_before: Sequence[Mapping[Any, Any]],
    ) -> bool:
        groups_after = self._groups(stream_name)
        return _group_snapshot(groups_before) == _group_snapshot(groups_after)

    @staticmethod
    def _inspect_entries(
        entries: Sequence[tuple[Any, Mapping[Any, Any]]],
        *,
        complete: bool,
    ) -> UnsettledDeliveryInspection:
        oldest_stream_id: str | None = None
        oldest_occurred_at: datetime | None = None
        message_keys: set[tuple[str, UUID]] = set()
        for raw_stream_id, fields in entries:
            stream_id = _as_text(raw_stream_id)
            oldest_stream_id = _minimum_stream_id(oldest_stream_id, stream_id)
            try:
                raw_envelope = _mapping_value(fields, "envelope")
                if isinstance(raw_envelope, bytes):
                    raw_envelope = raw_envelope.decode("utf-8")
                value = json.loads(raw_envelope) if isinstance(raw_envelope, str) else raw_envelope
                if not isinstance(value, dict):
                    raise TypeError("stream envelope must be a JSON object")
                envelope = MessageEnvelope.from_dict(value)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                complete = False
                continue
            occurred_at = _as_utc(envelope.occurred_at)
            if oldest_occurred_at is None or occurred_at < oldest_occurred_at:
                oldest_occurred_at = occurred_at
            message_keys.add((envelope.tenant_id, envelope.message_id))
        return UnsettledDeliveryInspection(
            oldest_stream_id=oldest_stream_id,
            oldest_occurred_at=oldest_occurred_at,
            message_keys=tuple(sorted(message_keys, key=lambda key: (key[0], str(key[1])))),
            scanned_entries=len(entries),
            complete=complete,
        )

    def _groups(self, stream_name: str) -> list[Mapping[Any, Any]]:
        try:
            return list(self._redis.xinfo_groups(stream_name))
        except ResponseError as exc:
            if "no such key" in str(exc).lower():
                return []
            raise

    def _candidate_ids(self, stream_name: str, *, before_id: str, count: int) -> list[str]:
        if count <= 0 or before_id == "0-0":
            return []
        maximum = "+" if before_id == "+" else f"({before_id}"
        entries = self._redis.xrange(
            stream_name,
            min="-",
            max=maximum,
            count=count,
        )
        return [_as_text(entry[0]) for entry in entries]

    def _measure_stream(
        self,
        *,
        policy: StreamRetentionPolicy,
        now: datetime,
        deleted_entries: int,
        groups: Sequence[Mapping[Any, Any]],
        pending_entries: int,
        oldest_pending_id: str | None,
        safe_group_floor_id: str | None,
        skipped_reason: str | None,
    ) -> StreamRetentionMetrics:
        length = int(self._redis.xlen(policy.stream_name))
        oldest = self._redis.xrange(policy.stream_name, min="-", max="+", count=1)
        oldest_id = _as_text(oldest[0][0]) if oldest else None
        return StreamRetentionMetrics(
            stream_name=policy.stream_name,
            deleted_entries=deleted_entries,
            length=length,
            max_entries=policy.max_entries,
            over_capacity_entries=max(0, length - policy.max_entries),
            consumer_groups=len(groups),
            pending_entries=pending_entries,
            oldest_entry_age_seconds=_stream_id_age_seconds(now, oldest_id),
            oldest_pending_age_seconds=_stream_id_age_seconds(now, oldest_pending_id),
            safe_group_floor_id=safe_group_floor_id,
            skipped_reason=skipped_reason,
        )


class MessagingRetentionService:
    def __init__(
        self,
        *,
        database: SqlAlchemyMessageRetentionStore,
        streams: RedisStreamRetentionStore,
        policy: MessagingRetentionPolicy,
    ) -> None:
        self._database = database
        self._streams = streams
        self._policy = policy

    def run_once(self, *, now: datetime | None = None) -> RetentionReport | None:
        observed_at = _as_utc(now or datetime.now(timezone.utc))
        with self._database.try_cycle_lock() as acquired:
            if not acquired:
                return None
            return self._run_locked(observed_at)

    def _run_locked(self, observed_at: datetime) -> RetentionReport:
        inbox_cutoff = observed_at - self._policy.inbox_retention
        inspections = [
            self._streams.inspect_unsettled(
                stream_policy,
                scan_limit=self._policy.batch_size,
            )
            for stream_policy in self._policy.streams
            if stream_policy.protects_inbox
        ]
        inbox_guard_scan_complete = all(inspection.complete for inspection in inspections)
        inbox_guard_scanned_entries = sum(inspection.scanned_entries for inspection in inspections)
        protected_message_keys = tuple(
            sorted(
                {key for inspection in inspections for key in inspection.message_keys},
                key=lambda key: (key[0], str(key[1])),
            )
        )
        oldest_unsettled_occurred_at = min(
            (
                inspection.oldest_occurred_at
                for inspection in inspections
                if inspection.oldest_occurred_at is not None
            ),
            default=None,
        )
        oldest_unsettled_delivery_age_seconds = _age_seconds(
            observed_at, oldest_unsettled_occurred_at
        )
        inbox_cleanup_blocked = not inbox_guard_scan_complete
        stream_metrics = tuple(
            self._streams.prune(
                policy=stream_policy,
                now=observed_at,
                batch_size=self._policy.batch_size,
            )
            for stream_policy in self._policy.streams
        )
        outbox_deleted = self._database.prune_outbox(
            cutoff=observed_at - self._policy.outbox_retention,
            batch_size=self._policy.batch_size,
        )
        inbox_deleted = 0
        if not inbox_cleanup_blocked:
            inbox_deleted = self._database.prune_inbox(
                cutoff=inbox_cutoff,
                batch_size=self._policy.batch_size,
                protected_message_keys=protected_message_keys,
            )
        database_metrics = self._database.measure(
            now=observed_at,
            outbox_deleted=outbox_deleted,
            inbox_deleted=inbox_deleted,
            inbox_cleanup_blocked=inbox_cleanup_blocked,
            inbox_guard_scan_complete=inbox_guard_scan_complete,
            inbox_guard_scanned_entries=inbox_guard_scanned_entries,
            inbox_guard_protected_messages=len(protected_message_keys),
            oldest_unsettled_delivery_age_seconds=(oldest_unsettled_delivery_age_seconds),
        )
        return RetentionReport(
            observed_at=observed_at,
            database=database_metrics,
            streams=stream_metrics,
        )


class RetentionScheduler:
    def __init__(
        self,
        *,
        service: MessagingRetentionService,
        interval: timedelta,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._interval_seconds = interval.total_seconds()
        self._monotonic = monotonic
        self._next_run_at: float | None = None

    def run_if_due(self) -> RetentionReport | None:
        current = self._monotonic()
        if self._next_run_at is not None and current < self._next_run_at:
            return None
        try:
            return self._service.run_once()
        finally:
            self._next_run_at = self._monotonic() + self._interval_seconds


def _mapping_value(
    value: Mapping[Any, Any],
    key: str,
    default: Any = None,
) -> Any:
    if key in value:
        return value[key]
    encoded = key.encode("utf-8")
    return value.get(encoded, default)


def _group_snapshot(
    groups: Sequence[Mapping[Any, Any]],
) -> tuple[tuple[str, int, str, str, str], ...]:
    return tuple(
        sorted(
            (
                _as_text(_mapping_value(group, "name")),
                int(_mapping_value(group, "pending", 0)),
                _as_text(_mapping_value(group, "last-delivered-id", "0-0")),
                _as_text(_mapping_value(group, "entries-read", "unknown")),
                _as_text(_mapping_value(group, "lag", "unknown")),
            )
            for group in groups
        )
    )


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_seconds(now: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    return max(0.0, (_as_utc(now) - _as_utc(value)).total_seconds())


def _datetime_stream_id(value: datetime) -> str:
    return f"{int(_as_utc(value).timestamp() * 1_000)}-0"


def _stream_id_parts(value: str) -> tuple[int, int]:
    milliseconds, sequence = value.split("-", maxsplit=1)
    return int(milliseconds), int(sequence)


def _minimum_stream_id(left: str | None, right: str) -> str:
    if left is None or _stream_id_parts(right) < _stream_id_parts(left):
        return right
    return left


def _stream_id_age_seconds(now: datetime, stream_id: str | None) -> float | None:
    if stream_id is None:
        return None
    return _age_seconds(now, _stream_id_datetime(stream_id))


def _stream_id_datetime(stream_id: str) -> datetime:
    milliseconds, _sequence = _stream_id_parts(stream_id)
    return datetime.fromtimestamp(milliseconds / 1_000, tz=timezone.utc)
