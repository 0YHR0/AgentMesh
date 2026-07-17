# Messaging retention and cleanup

Status: Accepted for implementation increment
Owners: AgentMesh maintainers
Depends on: [Durable asynchronous execution](durable-async-execution.md), [Event Relay](formal/event-relay.md), [Persistence and consistency](formal/persistence-and-consistency.md)

## 1. Scope

This increment bounds the durable messaging stores used by the current execution path. The Relay
periodically removes eligible PostgreSQL Outbox and Inbox rows, applies age and capacity policies
to Redis Streams, emits a structured maintenance report, and exposes Prometheus metrics. It does
not turn Redis into a business ledger or implement archive, replay, or broker-loss recovery.

Retention is part of the reliable core and is enabled in every feature profile. One Relay cycle
runs immediately after startup and later cycles use a monotonic interval.

## 2. Default guarantees

| Store | Default policy | Guarantee boundary |
|---|---:|---|
| Published Outbox | 7 days | Published delivery history remains available for this recovery window |
| Inbox | 30 days | Normal duplicate delivery with the same tenant, consumer, and message ID is deduplicated for at least this window |
| Execution and domain streams | 7 days and 100,000 entries | Only entries below every consumer-group safety floor may be removed |
| Dead-letter stream | 30 days and 50,000 entries | No implicit replay is performed; future replay must remain inside the Inbox horizon or be an explicitly authorized re-execution |
| One maintenance cycle | 1,000 records per store/stream | Work remains bounded and resumes oldest-first on the next cycle |

Startup validation enforces these horizon relationships:

```text
Redis Stream retention <= Published Outbox retention <= Inbox retention
Dead-letter retention <= Inbox retention
```

The Inbox duration is the ordinary deduplication promise, not permanent idempotency. A future
operator replay beyond that horizon must be modeled as an authorized new execution rather than a
transparent retry.

PostgreSQL durations are minimum windows before a row becomes eligible. Redis age and capacity
are alternative upper bounds: an already-safe entry may be removed earlier when the Stream is over
capacity. Pending and unread execution entries remain protected even after both bounds are crossed.

## 3. PostgreSQL cleanup

`SqlAlchemyMessageRetentionStore` owns short maintenance transactions outside the business Unit
of Work.

- Outbox cleanup selects only `PUBLISHED` rows whose non-null `published_at` is older than the
  cutoff. `PENDING` and `QUARANTINED` rows are never automatically deleted.
- Inbox cleanup selects only rows whose `processed_at` is older than the cutoff, whose exact
  message identity is not protected by an unsettled delivery, and for which no matching Outbox
  row still exists.
- Each selector orders by its retention timestamp and stable primary key, limits the batch, and
  uses `FOR UPDATE SKIP LOCKED` before `DELETE ... RETURNING`.
- A crash rolls back the current batch. A PostgreSQL session advisory lock elects one Relay for a
  maintenance cycle, preventing concurrent capacity decisions from over-deleting Redis history;
  `SKIP LOCKED` still keeps the row operations safe for manual or future partitioned workers.

Migration `20260717_0010` adds a partial Published-Outbox cleanup index, replaces the Inbox
single-column index with a stable covering retention index, makes the Inbox primary key
`tenant_id + consumer_name + message_id`, and enforces that `PUBLISHED` status and `published_at`
are present together.

The primary-key change is a coordinated schema transition, not a mixed-version rolling migration:
pause Inbox writers, apply `0010`, deploy all readers and writers with the tenant-aware key, and
then resume traffic. Downgrade is possible only while `(consumer_name, message_id)` remains unique
across tenants. The migration performs a preflight and rejects downgrade with an actionable error
rather than deleting or merging tenant data.

## 4. Inbox safety guard

Before deleting Inbox rows, maintenance performs a bounded inspection of pending and unread work
for every consumer group on the execution Stream. It parses at most one configured batch of
unsettled references per protected Stream and extracts each envelope's `(tenant_id, message_id)`.
A complete scan protects only those exact identities, so unrelated expired Inbox rows can still be
removed under continuous recent traffic. `occurred_at` is used for the oldest-unsettled age metric,
not as a deletion authorization signal.

If the reference bound is exceeded, an envelope is malformed, a group snapshot changes during the
inspection, or the controlled group count exceeds the scan bound, the scan is incomplete and Inbox
cleanup fails closed for that cycle. When the required group is missing, existing Stream entries
are treated as unsettled and scanned up to the same bound. Missing-group Stream cleanup also fails
closed, so entries remain until the configured group is present.

This is a safety-biased soft retention bound, not permission to discard active work. If unsettled
references continuously exceed the scan batch, Inbox cleanup pauses and storage can exceed the
target horizon. Operators must alert on scan-complete/blocked/overflow metrics, drain the backlog,
or raise the bounded batch within the configured limit. A resumable durable protection index is
deferred for deployments that need cleanup progress during larger active backlogs.

The Inbox delete statement independently excludes every matching Outbox row, regardless of status.
That database-side predicate closes the race in which another Relay publishes an old envelope
after the Redis inspection but before the delete. Together, exact Stream identities and the Outbox
predicate close the `business commit -> failed XACK -> very late reclaim` window without globally
blocking cleanup merely because work exists.

This guarantee assumes execution messages are emitted through the transactional Outbox. Direct
Redis injection is outside the reliable path. Creating, deleting, or resetting consumer groups
must be performed in a maintenance window with retention paused; runtime group-progress changes
detected during a cycle fail closed.

## 5. Redis retention

The implementation deliberately does not use unconditional `XADD MAXLEN` or `XTRIM MAXLEN`.
Those commands can remove a payload that is still referenced by a consumer group's pending-entry
list.

For every existing group, maintenance computes a conservative floor from the group's
`last-delivered-id` and, when present, its earliest pending ID. It uses the lower of those two
values, then the minimum floor across all groups. This protects both ordinary pending/unread work
and unread ranges exposed by a controlled `XGROUP SETID` rollback.

Age cleanup removes entries older than both the configured time cutoff and the group floor.
Capacity cleanup removes the oldest entries only when they are strictly below that same floor.
Deletion is exact, oldest-first, and limited to one configured batch. Pending and unread payloads
therefore survive even when the Stream remains above its soft maximum; the overflow is observable
instead of being converted into silent message loss.

The domain-event and dead-letter Streams currently have no internal consumer group, so their
age/capacity contracts apply directly. If a group is added later, the same discovery logic begins
protecting its progress automatically.

## 6. Scheduling and failure behavior

The existing `agentmesh-relay` process owns the lightweight scheduler and reuses its PostgreSQL
engine and Redis client. No extra service is required.

- the scheduler checks every Relay loop, so sustained publication cannot starve maintenance;
- the next due time is calculated after an attempt, including a failed attempt;
- a cleanup exception increments the failure metric and is logged, but does not terminate or
  pause Outbox publication;
- each database store and Stream processes at most one batch per cycle.

## 7. Operational metrics

The Relay exposes Prometheus text format on `http://localhost:9464/metrics` in Compose. Configure
the bind address and port with `AGENTMESH_RELAY_METRICS_HOST` and
`AGENTMESH_RELAY_METRICS_PORT`; set `AGENTMESH_RELAY_METRICS_ENABLED=false` to disable the HTTP
listener. Compose applies the configured port to both sides of the loopback-only mapping. The
cycle report is also written as a content-free structured log.

Metrics include:

- Outbox totals by status, Inbox rows, and oldest retained/pending ages;
- Stream length, configured maximum, protected overflow, pending count, oldest entry, and oldest
  pending age;
- Inbox guard scan-complete, scanned-entry, protected-message, and cleanup-blocked gauges;
- Stream cleanup-blocked gauges;
- deleted totals, maintenance failures, and last-success timestamp.

Stream names are deployment-level labels. Tenant IDs and message IDs are never metric labels, and
message envelopes are never logged or exported.

Deletion counters describe successfully reported cycles and are operational telemetry, not an
audit ledger. If a later operation or measurement fails after an earlier store committed its
batch, the failure counter advances but that partial deletion may not be reflected in the process
counter.

## 8. Configuration

| Setting | Default |
|---|---:|
| `AGENTMESH_RETENTION_INTERVAL_SECONDS` | `300` |
| `AGENTMESH_RETENTION_BATCH_SIZE` | `1000` |
| `AGENTMESH_OUTBOX_RETENTION_SECONDS` | `604800` |
| `AGENTMESH_INBOX_RETENTION_SECONDS` | `2592000` |
| `AGENTMESH_REDIS_STREAM_RETENTION_SECONDS` | `604800` |
| `AGENTMESH_REDIS_STREAM_MAX_ENTRIES` | `100000` |
| `AGENTMESH_DEAD_LETTER_STREAM_RETENTION_SECONDS` | `2592000` |
| `AGENTMESH_DEAD_LETTER_STREAM_MAX_ENTRIES` | `50000` |
| `AGENTMESH_RELAY_METRICS_ENABLED` | `true` |
| `AGENTMESH_RELAY_METRICS_HOST` | `0.0.0.0` |
| `AGENTMESH_RELAY_METRICS_PORT` | `9464` |

All durations, batch sizes, and capacities must be positive. The cleanup batch is capped at
10,000 records, the metrics port must be in `1..65535`, and the execution, domain-event, and
dead-letter Stream names must be distinct.

## 9. Verification

Fast tests cover horizon validation, pending floors, missing-group fail-closed behavior, exact and
overflow scan bounds, malformed envelopes, multiple groups, snapshot changes, bounded capacity
cleanup, the Inbox guard, scheduler failure isolation, and Prometheus output. The real
PostgreSQL/Redis tests prove:

- one-cycle batch limits and resumable cleanup;
- old Published Outbox rows are deleted while recent Published, Pending, and Quarantined rows
  remain;
- a fresh Redis ID carrying an old logical message protects its exact Inbox key while unrelated
  expired rows are still deleted;
- a matching Outbox row protects its Inbox key across publication races;
- an old acknowledged Stream entry is deleted while a pending payload remains;
- a recently retained message is actually redelivered and produces no duplicate side effect;
- migration upgrade, downgrade, re-upgrade, and model drift checks pass while the legacy two-column
  key remains unique; cross-tenant conflicts are rejected by the downgrade preflight.

## 10. Deferred work

- archival before deletion and tenant/consumer-specific retention policy;
- authorized Outbox/quarantine/dead-letter inspect and replay operations;
- recovery wakeup reconstruction after Redis loss;
- alerts and dashboards consuming the exported metrics;
- a dedicated maintenance deployment for isolation from Relay publication latency;
- pipelined unsettled-payload reads and a paginatable registry if deployments require many consumer
  groups (`XINFO GROUPS` itself is not paginated);
- a resumable durable unsettled-message protection index so Inbox cleanup can progress safely while
  active references exceed one scan batch;
- partitioning for sustained high-volume deployments.
