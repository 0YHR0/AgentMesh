# Controlled A2A remote cancellation

Status: Implemented baseline
Last updated: 2026-07-21

## Scope

AgentMesh can request cancellation of a known outbound A2A 1.0 Task through
`POST /api/v1/a2a/delegations/{correlation_id}/cancel`. The operation requires the existing
`a2a_delegation` Feature Gate, `a2a:delegate` and `task:operate` permissions, an authenticated
tenant Principal, a bounded reason, and an `Idempotency-Key`.

This is best-effort remote cancellation, not a local force-kill. The A2A server can report that the
Task is still active or that it completed before cancellation won the race. AgentMesh changes its
local Task and Run to canceled only after the remote Task reports `TASK_STATE_CANCELED`.

## Durable delivery state

The cancellation request digest, request count and first intent timestamp are committed before
network I/O. The same idempotency key returns the stored correlation and never repeats egress.

- `CANCELING` means the durable intent exists and the request is in flight, or the sender crashed
  while holding its short lease.
- `CANCEL_PENDING` means the remote Task was still active after the request.
- `CANCEL_OUTCOME_UNKNOWN` means the request may have arrived but no trustworthy response was
  received.

All three states retain the remote Task ID and converge through the existing Get Task reconciler.
An expired `CANCELING` lease is recovered by polling rather than by resending cancellation. A
failure proven to occur before delivery returns to `WAITING_REMOTE`; an operator may then make a
new explicit attempt with a new idempotency key.

## Protocol and convergence

The pinned HTTPS adapter sends A2A HTTP+JSON `POST /tasks/{id}:cancel`, including the optional
tenant route/body field and workload-bound Bearer credential. Redirects, private-address DNS,
unbounded payloads and credential passthrough remain rejected.

Only the observed remote terminal state determines the result:

- `TASK_STATE_CANCELED` cancels the local Task and Run.
- `TASK_STATE_COMPLETED` preserves and completes with the actual result.
- `TASK_STATE_FAILED` or `TASK_STATE_REJECTED` preserves that failure outcome.
- active states schedule another poll; input/auth/unknown states require intervention.

Local terminal state always wins over a late remote update; the correlation retains the late
evidence without overwriting the local result.

## Deliberate limits

This increment does not add bulk cancellation, inbound A2A serving, process force-kill, streaming,
push callbacks or automatic recovery of an initial Send Message with no known remote Task ID.
