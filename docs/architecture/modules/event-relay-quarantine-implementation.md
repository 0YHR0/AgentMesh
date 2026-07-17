# Event Relay poison-row quarantine

Status: Accepted for implementation increment
Owners: AgentMesh maintainers
Depends on: [Event Relay](formal/event-relay.md), [Persistence and consistency](formal/persistence-and-consistency.md)

## 1. Scope

This increment prevents one malformed PostgreSQL Outbox envelope from terminating the Event Relay
or blocking valid events behind it. It adds a durable terminal quarantine state to the existing
Outbox table; it does not implement the complete replay, retention, backpressure, or operator API
from the formal Event Relay target.

## 2. Claim and quarantine contract

`SqlAlchemyOutboxStore.claim_batch()` continues to claim available `PENDING` rows with
`FOR UPDATE SKIP LOCKED`. Each claimed row is deserialized independently inside that same database
transaction:

- a valid envelope remains claimed and is returned to `OutboxRelay` for publication;
- an invalid envelope becomes `QUARANTINED` immediately;
- quarantine clears claim ownership, records `quarantined_at`, increments `attempt_count`, and
  stores a bounded error category containing the exception type but not the envelope payload;
- other valid rows in the selected batch remain publishable.

`QUARANTINED` is terminal in this increment. It is excluded from normal claiming because the
claimer selects only `PENDING` rows. A future replay operation must perform an explicit audited
transition after the stored envelope has been repaired or upcast; restarting the Relay cannot
implicitly retry a poison row.

## 3. Persistence

Alembic revision `20260717_0009` adds nullable `outbox_events.quarantined_at` and a check constraint:

```text
status = QUARANTINED  <=>  quarantined_at is present
```

The existing `status`, `attempt_count`, `last_error`, tenant, topic, envelope, and timestamps make
the row inspectable in PostgreSQL. Until an operator API exists, maintainers can inspect metadata
without selecting payloads:

```sql
SELECT id, tenant_id, topic, attempt_count, quarantined_at, last_error
FROM outbox_events
WHERE status = 'QUARANTINED'
ORDER BY quarantined_at DESC;
```

## 4. Failure and security behavior

- If the claim/quarantine transaction fails, neither claims nor quarantine changes commit; normal
  database retry behavior applies.
- Publishing and marking valid rows retain the existing at-least-once semantics.
- The persisted error intentionally omits `str(exception)` because validation errors can echo
  untrusted or sensitive payload fragments. Operators can inspect the protected source row when
  authorized.
- A malformed row consumes one claim attempt and then leaves the `PENDING` claim set permanently.

## 5. Verification

The PostgreSQL integration regression inserts a malformed row immediately before a valid row in
the same claimed batch. It proves that the Relay returns normally, publishes the valid envelope,
durably quarantines the malformed row, clears its claim, and records no payload in the error.

Migration CI additionally proves upgrade, downgrade by one revision, and re-upgrade. Compose E2E
continues to prove the normal PostgreSQL Outbox to Redis Streams path.

## 6. Deferred work

- authorized inspect/replay APIs and audit entries;
- schema upcasters and route-level compatibility pauses;
- alerting and quarantine/backlog metrics;
- bounded Outbox/Inbox/Redis retention tracked by
  [#14](https://github.com/0YHR0/AgentMesh/issues/14).
