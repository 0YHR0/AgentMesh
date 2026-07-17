from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from prometheus_client import generate_latest

from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.maintenance.metrics import PrometheusRetentionMetrics
from agentmesh.maintenance.retention import (
    DatabaseRetentionMetrics,
    MessagingRetentionPolicy,
    MessagingRetentionService,
    RedisStreamRetentionStore,
    RetentionScheduler,
    StreamRetentionMetrics,
    StreamRetentionPolicy,
    UnsettledDeliveryInspection,
)


def test_stream_retention_preserves_pending_floor() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    old_acked = _stream_id(now - timedelta(days=10))
    old_pending = _stream_id(now - timedelta(days=9))
    recent = _stream_id(now - timedelta(days=1))
    redis = _FakeRedis(
        entries=[old_acked, old_pending, recent],
        groups=[
            {
                "name": "workers",
                "pending": 1,
                "last-delivered-id": old_pending,
            },
            {
                "name": "projection",
                "pending": 0,
                "last-delivered-id": recent,
            },
        ],
        pending={"workers": [old_pending]},
    )

    metrics = RedisStreamRetentionStore(redis).prune(  # type: ignore[arg-type]
        policy=StreamRetentionPolicy(
            stream_name="runs",
            retention=timedelta(days=7),
            max_entries=2,
            required_group="workers",
        ),
        now=now,
        batch_size=100,
    )

    assert redis.entries == [old_pending, recent]
    assert metrics.deleted_entries == 1
    assert metrics.length == 2
    assert metrics.pending_entries == 1
    assert metrics.safe_group_floor_id == old_pending
    assert metrics.oldest_pending_age_seconds == pytest.approx(9 * 24 * 60 * 60)
    assert metrics.over_capacity_entries == 0


def test_stream_reports_protected_overflow_instead_of_deleting_pending_work() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    pending = _stream_id(now - timedelta(days=10))
    unread = [
        _stream_id(now - timedelta(days=9)),
        _stream_id(now - timedelta(days=8)),
    ]
    redis = _FakeRedis(
        entries=[pending, *unread],
        groups=[
            {
                "name": "workers",
                "pending": 1,
                "last-delivered-id": pending,
            }
        ],
        pending={"workers": [pending]},
    )

    metrics = RedisStreamRetentionStore(redis).prune(  # type: ignore[arg-type]
        policy=StreamRetentionPolicy(
            stream_name="runs",
            retention=timedelta(days=7),
            max_entries=1,
            required_group="workers",
        ),
        now=now,
        batch_size=100,
    )

    assert redis.entries == [pending, *unread]
    assert metrics.deleted_entries == 0
    assert metrics.over_capacity_entries == 2


def test_stream_retention_preserves_unread_range_after_group_id_rollback() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    old_acked = _stream_id(now - timedelta(days=12))
    reset_anchor = _stream_id(now - timedelta(days=11))
    unread_after_reset = _stream_id(now - timedelta(days=10))
    pending = _stream_id(now - timedelta(days=9))
    redis = _FakeRedis(
        entries=[old_acked, reset_anchor, unread_after_reset, pending],
        groups=[
            {
                "name": "workers",
                "pending": 1,
                "last-delivered-id": reset_anchor,
            }
        ],
        pending={"workers": [pending]},
    )

    metrics = RedisStreamRetentionStore(redis).prune(  # type: ignore[arg-type]
        policy=StreamRetentionPolicy(
            stream_name="runs",
            retention=timedelta(days=7),
            max_entries=1,
            required_group="workers",
        ),
        now=now,
        batch_size=100,
    )

    assert redis.entries == [reset_anchor, unread_after_reset, pending]
    assert metrics.deleted_entries == 1
    assert metrics.safe_group_floor_id == reset_anchor


def test_stream_retention_fails_closed_when_required_group_is_missing() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    old = _stream_id(now - timedelta(days=30))
    redis = _FakeRedis(entries=[old])

    metrics = RedisStreamRetentionStore(redis).prune(  # type: ignore[arg-type]
        policy=StreamRetentionPolicy(
            stream_name="runs",
            retention=timedelta(days=7),
            max_entries=1,
            required_group="workers",
        ),
        now=now,
        batch_size=100,
    )

    assert redis.entries == [old]
    assert metrics.deleted_entries == 0
    assert metrics.skipped_reason == "required_consumer_group_missing"


def test_stream_capacity_cleanup_is_bounded_without_consumer_groups() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    entries = [_stream_id(now - timedelta(seconds=offset)) for offset in (4, 3, 2, 1)]
    redis = _FakeRedis(entries=entries)
    store = RedisStreamRetentionStore(redis)  # type: ignore[arg-type]
    policy = StreamRetentionPolicy(
        stream_name="events",
        retention=timedelta(days=7),
        max_entries=1,
    )

    first = store.prune(policy=policy, now=now, batch_size=2)
    second = store.prune(policy=policy, now=now, batch_size=2)

    assert first.deleted_entries == 2
    assert first.over_capacity_entries == 1
    assert second.deleted_entries == 1
    assert redis.entries == [entries[-1]]


def test_oldest_unsettled_delivery_includes_pending_and_unread_entries() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    acked = _stream_id(now - timedelta(days=4))
    pending = _stream_id(now - timedelta(days=3))
    unread = _stream_id(now - timedelta(days=2))
    pending_envelope = _envelope("tenant-a", now - timedelta(days=31))
    unread_envelope = _envelope("tenant-a", now - timedelta(days=1))
    redis = _FakeRedis(
        entries=[acked, pending, unread],
        groups=[
            {
                "name": "workers",
                "pending": 1,
                "last-delivered-id": pending,
            }
        ],
        pending={"workers": [pending]},
        envelopes={
            pending: pending_envelope.to_dict(),
            unread: unread_envelope.to_dict(),
        },
    )
    policy = StreamRetentionPolicy(
        stream_name="runs",
        retention=timedelta(days=1),
        max_entries=10,
        required_group="workers",
        protects_inbox=True,
    )

    inspection = RedisStreamRetentionStore(redis).inspect_unsettled(  # type: ignore[arg-type]
        policy,
        scan_limit=10,
    )

    assert inspection.complete is True
    assert inspection.oldest_stream_id == pending
    assert inspection.oldest_occurred_at == now - timedelta(days=31)
    assert set(inspection.message_keys) == {
        (pending_envelope.tenant_id, pending_envelope.message_id),
        (unread_envelope.tenant_id, unread_envelope.message_id),
    }
    assert inspection.scanned_entries == 2


def test_unsettled_scan_fails_closed_when_work_exceeds_the_bound() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    first = _stream_id(now - timedelta(days=2))
    second = _stream_id(now - timedelta(days=1))
    redis = _FakeRedis(
        entries=[first, second],
        envelopes={
            first: _envelope("tenant-a", now - timedelta(days=2)).to_dict(),
            second: _envelope("tenant-a", now - timedelta(days=1)).to_dict(),
        },
    )

    inspection = RedisStreamRetentionStore(redis).inspect_unsettled(  # type: ignore[arg-type]
        StreamRetentionPolicy(
            stream_name="runs",
            retention=timedelta(days=7),
            max_entries=10,
            required_group="workers",
            protects_inbox=True,
        ),
        scan_limit=1,
    )

    assert inspection.complete is False
    assert inspection.scanned_entries == 1


def test_missing_group_scan_completes_at_the_exact_bound() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    message_id = _stream_id(now - timedelta(days=1))
    envelope = _envelope("tenant-a", now - timedelta(days=31))
    redis = _FakeRedis(
        entries=[message_id],
        envelopes={message_id: envelope.to_dict()},
    )

    inspection = RedisStreamRetentionStore(redis).inspect_unsettled(  # type: ignore[arg-type]
        StreamRetentionPolicy(
            stream_name="runs",
            retention=timedelta(days=7),
            max_entries=10,
            required_group="workers",
            protects_inbox=True,
        ),
        scan_limit=1,
    )

    assert inspection.complete is True
    assert inspection.message_keys == (("tenant-a", envelope.message_id),)


def test_unsettled_scan_fails_closed_for_malformed_envelope() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    pending = _stream_id(now - timedelta(days=1))
    redis = _FakeRedis(
        entries=[pending],
        groups=[
            {
                "name": "workers",
                "pending": 1,
                "last-delivered-id": pending,
            }
        ],
        pending={"workers": [pending]},
    )

    inspection = RedisStreamRetentionStore(redis).inspect_unsettled(  # type: ignore[arg-type]
        StreamRetentionPolicy(
            stream_name="runs",
            retention=timedelta(days=7),
            max_entries=10,
            required_group="workers",
            protects_inbox=True,
        ),
        scan_limit=10,
    )

    assert inspection.complete is False
    assert inspection.scanned_entries == 1
    assert inspection.message_keys == ()


def test_unsettled_scan_covers_pending_and_unread_work_across_groups() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    first = _stream_id(now - timedelta(days=3))
    second = _stream_id(now - timedelta(days=2))
    third = _stream_id(now - timedelta(days=1))
    envelopes = {
        message_id: _envelope("tenant-a", occurred_at).to_dict()
        for message_id, occurred_at in (
            (first, now - timedelta(days=33)),
            (second, now - timedelta(days=2)),
            (third, now - timedelta(days=1)),
        )
    }
    redis = _FakeRedis(
        entries=[first, second, third],
        groups=[
            {"name": "workers", "pending": 1, "last-delivered-id": first},
            {"name": "projection", "pending": 1, "last-delivered-id": second},
        ],
        pending={"workers": [first], "projection": [second]},
        envelopes=envelopes,
    )

    inspection = RedisStreamRetentionStore(redis).inspect_unsettled(  # type: ignore[arg-type]
        StreamRetentionPolicy(
            stream_name="runs",
            retention=timedelta(days=7),
            max_entries=10,
            required_group="workers",
            protects_inbox=True,
        ),
        scan_limit=10,
    )

    assert inspection.complete is True
    assert inspection.scanned_entries == 3
    assert len(inspection.message_keys) == 3


def test_unsettled_scan_fails_closed_when_group_progress_changes() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    message_id = _stream_id(now - timedelta(days=1))
    envelope = _envelope("tenant-a", now - timedelta(days=1))
    redis = _FakeRedis(
        entries=[message_id],
        groups=[{"name": "workers", "pending": 0, "last-delivered-id": "0-0"}],
        groups_after=[{"name": "workers", "pending": 1, "last-delivered-id": message_id}],
        envelopes={message_id: envelope.to_dict()},
    )

    inspection = RedisStreamRetentionStore(redis).inspect_unsettled(  # type: ignore[arg-type]
        StreamRetentionPolicy(
            stream_name="runs",
            retention=timedelta(days=7),
            max_entries=10,
            required_group="workers",
            protects_inbox=True,
        ),
        scan_limit=10,
    )

    assert inspection.complete is False


def test_service_blocks_inbox_cleanup_when_unsettled_scan_is_incomplete() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    database = _FakeDatabase()
    streams = _FakeStreamStore(
        oldest_occurred_at=now - timedelta(days=31),
        complete=False,
    )
    service = MessagingRetentionService(
        database=database,  # type: ignore[arg-type]
        streams=streams,  # type: ignore[arg-type]
        policy=_messaging_policy(),
    )

    report = service.run_once(now=now)

    assert report is not None
    assert database.outbox_prunes == 1
    assert database.inbox_prunes == 0
    assert report.database.inbox_cleanup_blocked is True
    assert report.database.oldest_unsettled_delivery_age_seconds == pytest.approx(31 * 24 * 60 * 60)
    assert report.database.inbox_guard_scan_complete is False
    assert report.to_dict()["observed_at"] == "2026-07-17T00:00:00+00:00"


def test_service_prunes_inbox_while_protecting_exact_unsettled_messages() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    database = _FakeDatabase()
    protected_id = uuid4()
    streams = _FakeStreamStore(
        oldest_occurred_at=now - timedelta(days=31),
        message_keys=(("tenant-a", protected_id),),
    )
    service = MessagingRetentionService(
        database=database,  # type: ignore[arg-type]
        streams=streams,  # type: ignore[arg-type]
        policy=_messaging_policy(),
    )

    report = service.run_once(now=now)

    assert report is not None
    assert database.inbox_prunes == 1
    assert database.protected_message_keys == (("tenant-a", protected_id),)
    assert report.database.inbox_cleanup_blocked is False
    assert report.database.inbox_guard_protected_messages == 1
    assert report.database.oldest_unsettled_delivery_age_seconds == pytest.approx(31 * 24 * 60 * 60)


def test_service_skips_cycle_when_another_relay_holds_the_leader_lock() -> None:
    database = _FakeDatabase(lock_acquired=False)
    service = MessagingRetentionService(
        database=database,  # type: ignore[arg-type]
        streams=_FakeStreamStore(),  # type: ignore[arg-type]
        policy=_messaging_policy(),
    )

    assert service.run_once() is None
    assert database.outbox_prunes == 0
    assert database.inbox_prunes == 0


def test_scheduler_runs_immediately_and_defers_next_attempt_after_failure() -> None:
    clock = _Clock()
    service = _FailingService()
    scheduler = RetentionScheduler(
        service=service,  # type: ignore[arg-type]
        interval=timedelta(seconds=10),
        monotonic=clock,
    )

    with pytest.raises(RuntimeError, match="maintenance failed"):
        scheduler.run_if_due()
    assert scheduler.run_if_due() is None

    clock.value = 10
    assert scheduler.run_if_due() == "report"
    assert service.calls == 2


def test_prometheus_metrics_expose_retention_capacity_and_failures() -> None:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    service = MessagingRetentionService(
        database=_FakeDatabase(),  # type: ignore[arg-type]
        streams=_FakeStreamStore(  # type: ignore[arg-type]
            oldest_occurred_at=now - timedelta(days=1)
        ),
        policy=_messaging_policy(),
    )
    report = service.run_once(now=now)
    assert report is not None
    metrics = PrometheusRetentionMetrics()

    metrics.observe(report)
    metrics.record_failure()
    payload = generate_latest(metrics.registry).decode("utf-8")

    assert 'agentmesh_outbox_rows{status="pending"} 1.0' in payload
    assert 'agentmesh_redis_stream_length{stream="runs"} 0.0' in payload
    assert 'agentmesh_messaging_retention_deleted_total{store="outbox"} 2.0' in payload
    assert "agentmesh_inbox_guard_scan_complete 1.0" in payload
    assert "agentmesh_inbox_guard_scanned_entries 1.0" in payload
    assert "agentmesh_messaging_retention_failures_total 1.0" in payload


class _FakeRedis:
    def __init__(
        self,
        *,
        entries: list[str],
        groups: list[dict[str, Any]] | None = None,
        groups_after: list[dict[str, Any]] | None = None,
        pending: dict[str, list[str]] | None = None,
        envelopes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.entries = sorted(entries, key=_parts)
        self.groups = groups or []
        self.groups_after = groups_after
        self.group_reads = 0
        self.pending = pending or {}
        self.envelopes = envelopes or {}

    def xlen(self, _stream_name: str) -> int:
        return len(self.entries)

    def xinfo_groups(self, _stream_name: str) -> list[dict[str, Any]]:
        self.group_reads += 1
        if self.groups_after is not None and self.group_reads > 1:
            return self.groups_after
        return self.groups

    def xpending_range(
        self,
        _stream_name: str,
        group_name: str,
        *,
        min: str,
        max: str,
        count: int,
    ) -> list[dict[str, Any]]:
        del min, max
        return [
            {"message_id": message_id} for message_id in self.pending.get(group_name, [])[:count]
        ]

    def xrange(
        self,
        _stream_name: str,
        *,
        min: str,
        max: str,
        count: int,
    ) -> list[tuple[str, dict[str, str]]]:
        values = self.entries
        if min != "-":
            exclusive = min.startswith("(")
            lower = _parts(min[1:] if exclusive else min)
            values = [
                value
                for value in values
                if _parts(value) > lower or (not exclusive and _parts(value) == lower)
            ]
        if max != "+":
            exclusive = max.startswith("(")
            upper = _parts(max[1:] if exclusive else max)
            values = [
                value
                for value in values
                if _parts(value) < upper or (not exclusive and _parts(value) == upper)
            ]
        return [
            (
                value,
                {"envelope": json.dumps(self.envelopes.get(value, {}))},
            )
            for value in values[:count]
        ]

    def xdel(self, _stream_name: str, *message_ids: str) -> int:
        before = len(self.entries)
        selected = set(message_ids)
        self.entries = [entry for entry in self.entries if entry not in selected]
        return before - len(self.entries)


class _FakeDatabase:
    def __init__(
        self,
        *,
        lock_acquired: bool = True,
    ) -> None:
        self.outbox_prunes = 0
        self.inbox_prunes = 0
        self.lock_acquired = lock_acquired
        self.protected_message_keys: tuple[tuple[str, UUID], ...] = ()

    @contextmanager
    def try_cycle_lock(self) -> Iterator[bool]:
        yield self.lock_acquired

    def prune_outbox(self, *, cutoff: datetime, batch_size: int) -> int:
        del cutoff, batch_size
        self.outbox_prunes += 1
        return 2

    def prune_inbox(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
        protected_message_keys: tuple[tuple[str, UUID], ...],
    ) -> int:
        del cutoff, batch_size
        self.inbox_prunes += 1
        self.protected_message_keys = protected_message_keys
        return 3

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
        del now
        return DatabaseRetentionMetrics(
            outbox_deleted=outbox_deleted,
            inbox_deleted=inbox_deleted,
            outbox_rows=10,
            outbox_pending_rows=1,
            outbox_published_rows=8,
            outbox_quarantined_rows=1,
            inbox_rows=20,
            inbox_cleanup_blocked=inbox_cleanup_blocked,
            inbox_guard_scan_complete=inbox_guard_scan_complete,
            inbox_guard_scanned_entries=inbox_guard_scanned_entries,
            inbox_guard_protected_messages=inbox_guard_protected_messages,
            oldest_unsettled_delivery_age_seconds=(oldest_unsettled_delivery_age_seconds),
            oldest_outbox_pending_age_seconds=5,
            oldest_outbox_published_age_seconds=10,
            oldest_outbox_quarantined_age_seconds=15,
            oldest_inbox_age_seconds=20,
        )


class _FakeStreamStore:
    def __init__(
        self,
        *,
        oldest_occurred_at: datetime | None = None,
        message_keys: tuple[tuple[str, UUID], ...] = (),
        complete: bool = True,
    ) -> None:
        self.oldest_occurred_at = oldest_occurred_at
        self.message_keys = message_keys
        self.complete = complete

    def inspect_unsettled(
        self,
        _policy: StreamRetentionPolicy,
        *,
        scan_limit: int,
    ) -> UnsettledDeliveryInspection:
        del scan_limit
        return UnsettledDeliveryInspection(
            oldest_stream_id=None,
            oldest_occurred_at=self.oldest_occurred_at,
            message_keys=self.message_keys,
            scanned_entries=1 if self.oldest_occurred_at is not None else 0,
            complete=self.complete,
        )

    def prune(
        self,
        *,
        policy: StreamRetentionPolicy,
        now: datetime,
        batch_size: int,
    ) -> StreamRetentionMetrics:
        del now, batch_size
        return StreamRetentionMetrics(
            stream_name=policy.stream_name,
            deleted_entries=0,
            length=0,
            max_entries=policy.max_entries,
            over_capacity_entries=0,
            consumer_groups=1,
            pending_entries=0,
            oldest_entry_age_seconds=None,
            oldest_pending_age_seconds=None,
            safe_group_floor_id=None,
            skipped_reason=None,
        )


class _Clock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


class _FailingService:
    def __init__(self) -> None:
        self.calls = 0

    def run_once(self) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("maintenance failed")
        return "report"


def _messaging_policy() -> MessagingRetentionPolicy:
    stream = StreamRetentionPolicy(
        stream_name="runs",
        retention=timedelta(days=7),
        max_entries=100,
        required_group="workers",
        protects_inbox=True,
    )
    return MessagingRetentionPolicy(
        outbox_retention=timedelta(days=7),
        inbox_retention=timedelta(days=30),
        batch_size=100,
        streams=(stream,),
    )


def _envelope(tenant_id: str, occurred_at: datetime) -> MessageEnvelope:
    return replace(
        MessageEnvelope.run_requested(
            tenant_id=tenant_id,
            task_id=uuid4(),
            run_id=uuid4(),
        ),
        occurred_at=occurred_at,
    )


def _stream_id(value: datetime) -> str:
    return f"{int(value.timestamp() * 1_000)}-0"


def _parts(value: str) -> tuple[int, int]:
    milliseconds, sequence = value.split("-", maxsplit=1)
    return int(milliseconds), int(sequence)
