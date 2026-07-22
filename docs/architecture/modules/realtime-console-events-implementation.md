# Realtime Console events baseline

Status: Implemented baseline

## Outcome

The built-in Console can react to durable AgentMesh domain events without making the event Stream a
second data plane. A feature-gated Server-Sent Events endpoint projects tenant-safe invalidation
metadata, and the browser rereads the existing authorized API for the active workspace.

## Runtime flow

```text
PostgreSQL Outbox -> Event Relay -> Redis domain-event Stream
                                      |
                                      v
Control API tenant filter -> authenticated SSE -> active Console view refresh
                                                      |
                                                      v
                                          existing bounded REST API
```

The browser loads its initial snapshot before opening the Stream. It starts at `$` unless it has a
connection cursor, sends that cursor as `Last-Event-ID` on reconnect, and debounces bursts into one
active-view refresh. Stream entry IDs are used as cursors; heartbeat events advance the cursor past
malformed or other-tenant entries without exposing them.

## Security boundary

- The endpoint requires the `realtime_events` Gate and `system:inspect` permission.
- Authentication uses the same optional same-origin Bearer token as the rest of the Console.
- Every envelope is filtered by the immutable Principal tenant before emission.
- Domain events contain only message ID, schema name/version, occurrence time, and correlation ID.
  Domain payloads, Task output, Artifact content, Tool arguments/results, and credentials are never
  projected.
- An event is only an invalidation hint. Authoritative resource access remains on the normal REST
  routes, so their RBAC and feature checks remain the final boundary.

## Failure behavior

Minimal and standard profiles keep the existing three-second active-view poll. The full profile
opens the SSE connection and keeps a fifteen-second safety poll. Redis errors close the Stream after
an `unavailable` event; the browser shows polling fallback and retries with exponential backoff from
one to fifteen seconds. API and PostgreSQL-backed Console operations remain available during the
broker outage.

SSE delivery is resumable only within Redis Stream retention. This does not weaken UI correctness:
the initial snapshot and safety poll recover current state even when an old cursor has expired.

## Verification

Tests cover malformed Stream entries, cursor advancement, tenant filtering, payload redaction,
compact SSE encoding, disabled/unavailable endpoint behavior, and frontend authentication/cursor
wiring. Local full-profile smoke verification additionally covers near-realtime Artifact refresh,
Redis outage fallback, and automatic reconnection.

## Deferred

- per-resource subscription filters and connection capacity controls;
- SSE connection/lag metrics and proxy/load soak testing;
- a persisted cross-domain audit projection, which is separate from this invalidation transport.
