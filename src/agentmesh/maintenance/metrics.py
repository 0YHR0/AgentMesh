from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge

from agentmesh.maintenance.retention import RetentionReport


class PrometheusRetentionMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self._outbox_rows = Gauge(
            "agentmesh_outbox_rows",
            "Current PostgreSQL Outbox rows by status.",
            ("status",),
            registry=self.registry,
        )
        self._inbox_rows = Gauge(
            "agentmesh_inbox_rows",
            "Current PostgreSQL Inbox rows.",
            registry=self.registry,
        )
        self._oldest_record_age = Gauge(
            "agentmesh_messaging_oldest_record_age_seconds",
            "Age of the oldest retained messaging record.",
            ("record_type",),
            registry=self.registry,
        )
        self._stream_length = Gauge(
            "agentmesh_redis_stream_length",
            "Current Redis Stream length.",
            ("stream",),
            registry=self.registry,
        )
        self._stream_max_entries = Gauge(
            "agentmesh_redis_stream_max_entries",
            "Configured soft maximum entries for a Redis Stream.",
            ("stream",),
            registry=self.registry,
        )
        self._stream_pending = Gauge(
            "agentmesh_redis_stream_pending_entries",
            "Current pending entries across Redis consumer groups.",
            ("stream",),
            registry=self.registry,
        )
        self._stream_over_capacity = Gauge(
            "agentmesh_redis_stream_over_capacity_entries",
            "Entries above the soft maximum that are not yet safe to remove.",
            ("stream",),
            registry=self.registry,
        )
        self._stream_oldest_age = Gauge(
            "agentmesh_redis_stream_oldest_entry_age_seconds",
            "Age of the oldest retained Redis Stream entry.",
            ("stream",),
            registry=self.registry,
        )
        self._stream_oldest_pending_age = Gauge(
            "agentmesh_redis_stream_oldest_pending_age_seconds",
            "Age of the oldest pending Redis Stream entry.",
            ("stream",),
            registry=self.registry,
        )
        self._stream_cleanup_blocked = Gauge(
            "agentmesh_redis_stream_cleanup_blocked",
            "Whether stream cleanup was skipped because a safety precondition failed.",
            ("stream",),
            registry=self.registry,
        )
        self._inbox_cleanup_blocked = Gauge(
            "agentmesh_inbox_cleanup_blocked",
            "Whether an incomplete unsettled-delivery scan blocked Inbox cleanup.",
            registry=self.registry,
        )
        self._inbox_guard_scan_complete = Gauge(
            "agentmesh_inbox_guard_scan_complete",
            "Whether the unsettled-delivery guard completed its bounded scan.",
            registry=self.registry,
        )
        self._inbox_guard_scanned_entries = Gauge(
            "agentmesh_inbox_guard_scanned_entries",
            "Distinct unsettled Stream entries inspected during the latest cycle.",
            registry=self.registry,
        )
        self._inbox_guard_protected_messages = Gauge(
            "agentmesh_inbox_guard_protected_messages",
            "Inbox message identities protected by unsettled Stream deliveries.",
            registry=self.registry,
        )
        self._deleted = Counter(
            "agentmesh_messaging_retention_deleted_total",
            "Messaging records removed by retention maintenance.",
            ("store",),
            registry=self.registry,
        )
        self._failures = Counter(
            "agentmesh_messaging_retention_failures_total",
            "Retention maintenance cycles that failed.",
            registry=self.registry,
        )
        self._last_success = Gauge(
            "agentmesh_messaging_retention_last_success_timestamp_seconds",
            "Unix timestamp of the last successful retention maintenance cycle.",
            registry=self.registry,
        )

    def observe(self, report: RetentionReport) -> None:
        database = report.database
        self._outbox_rows.labels(status="all").set(database.outbox_rows)
        self._outbox_rows.labels(status="pending").set(database.outbox_pending_rows)
        self._outbox_rows.labels(status="published").set(database.outbox_published_rows)
        self._outbox_rows.labels(status="quarantined").set(database.outbox_quarantined_rows)
        self._inbox_rows.set(database.inbox_rows)
        self._set_optional(
            self._oldest_record_age.labels(record_type="outbox_pending"),
            database.oldest_outbox_pending_age_seconds,
        )
        self._set_optional(
            self._oldest_record_age.labels(record_type="outbox_published"),
            database.oldest_outbox_published_age_seconds,
        )
        self._set_optional(
            self._oldest_record_age.labels(record_type="outbox_quarantined"),
            database.oldest_outbox_quarantined_age_seconds,
        )
        self._set_optional(
            self._oldest_record_age.labels(record_type="inbox"),
            database.oldest_inbox_age_seconds,
        )
        self._set_optional(
            self._oldest_record_age.labels(record_type="unsettled_delivery"),
            database.oldest_unsettled_delivery_age_seconds,
        )
        self._inbox_cleanup_blocked.set(float(database.inbox_cleanup_blocked))
        self._inbox_guard_scan_complete.set(float(database.inbox_guard_scan_complete))
        self._inbox_guard_scanned_entries.set(database.inbox_guard_scanned_entries)
        self._inbox_guard_protected_messages.set(database.inbox_guard_protected_messages)
        self._deleted.labels(store="outbox").inc(database.outbox_deleted)
        self._deleted.labels(store="inbox").inc(database.inbox_deleted)

        for stream in report.streams:
            label = stream.stream_name
            self._stream_length.labels(stream=label).set(stream.length)
            self._stream_max_entries.labels(stream=label).set(stream.max_entries)
            self._stream_pending.labels(stream=label).set(stream.pending_entries)
            self._stream_over_capacity.labels(stream=label).set(stream.over_capacity_entries)
            self._set_optional(
                self._stream_oldest_age.labels(stream=label),
                stream.oldest_entry_age_seconds,
            )
            self._set_optional(
                self._stream_oldest_pending_age.labels(stream=label),
                stream.oldest_pending_age_seconds,
            )
            self._stream_cleanup_blocked.labels(stream=label).set(
                float(stream.skipped_reason is not None)
            )
            self._deleted.labels(store=f"redis:{label}").inc(stream.deleted_entries)

        self._last_success.set(report.observed_at.timestamp())

    def record_failure(self) -> None:
        self._failures.inc()

    @staticmethod
    def _set_optional(metric: Gauge, value: float | None) -> None:
        metric.set(float("nan") if value is None else value)
