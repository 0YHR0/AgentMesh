# L1 — Current deployment and ownership

Status: Current architecture summary · Updated: 2026-09-29

This is the current deployable shape of the single-team baseline, not a list of proposed
microservices. See [L0](L0-system-design.md) for the system boundary and
[implementation status](../implementation-status.md) for feature maturity.

## Deployment units

| Unit | Responsibility | Durable authority |
| --- | --- | --- |
| Control API and served Console | Validate commands, expose queries and UI, manage identity, policy, registries, and configuration | PostgreSQL transactions |
| Execution Worker | Claim fenced Attempts, run version-bound local agents, checkpoint work, submit candidate results | PostgreSQL business commands; LangGraph checkpoint for execution state |
| Event Relay | Publish committed Outbox work to Redis Streams; retry and quarantine failures | PostgreSQL Outbox/Inbox |
| PostgreSQL | Business ledger, checkpoints, idempotency, leases, and audit | System of record |
| Redis Streams | At-least-once work delivery | No business authority |

The default Compose deployment serves the Console from the API; there is no separate frontend
build. Optional Company scheduler, A2A reconciler, Langfuse export, MCP connections, and trusted
Runtime Extensions add capabilities only when configured. They do not replace the core ledger.

```text
Owner / Console / API client
            ↓
Control API → PostgreSQL Task + Run + Outbox
                               ↓
                         Event Relay → Redis Streams
                                               ↓
                                       Execution Worker
                                               ↓
                                PostgreSQL result + evidence
```

## Ownership and failure rules

- The API records a Task and Outbox message atomically. The Relay may publish more than once;
  consumers use Inbox/idempotency records and fencing to reject duplicates or stale Workers.
- A Run pins its Agent Version and execution authority. Flipping a Feature Gate does not silently
  move an in-flight Run to another runtime.
- MCP and A2A are outbound trust boundaries. Published tool/peer snapshots and policy, not live
  unreviewed discovery, determine what an Agent may use.
- A provider call with an uncertain external outcome is recorded as unknown for inspection and
  reconciliation, not blindly replayed.
- Model and tool credentials are bound through authenticated administration and never become
  Task input. The keyless deterministic demo is a separate first-run path.

Detailed current contracts live in the [L2 module index](modules/README.md). The formal L2 set
also contains future targets; use [implementation status](../implementation-status.md) to
distinguish them from shipped behavior. Architecture changes with cross-module consequences are
recorded in [ADRs](../adr/README.md).
