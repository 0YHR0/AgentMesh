# Runtime outcome reconciliation

This module is the evidence-driven, privileged exit for a managed DIRECT execution parked after
the provider dispatch boundary. It converges known provider evidence without executing the Agent
again.

## Admission and evidence

The API requires the managed-runtime and outcome-reconciliation feature gates, the
`outcome:reconcile` permission, an authenticated same-tenant Principal, and an idempotency key. It
does not require the direct-cutover gate because disabling new admission must not strand existing
work.

The request contains a complete public `RuntimeObservation`, its canonical digest, a bounded
evidence reference, and a bounded operator reason. Only `SUCCEEDED`, `FAILED`, `CANCELED`, and
`TIMED_OUT` observations are accepted. Execution, assignment, digest, phase, and provider identity
evidence must match the persisted Runtime execution. Success remains limited to mapping output and
empty usage; other terminal phases cannot carry successful output.

## Atomic convergence

After locating the execution without a lock, the service locks Task, Run, latest Attempt, then
RuntimeExecution and revalidates the complete parked quartet. In one UoW it records or reuses exact
immutable observation evidence, reconciles Runtime and business state, adds a TaskResolution and
`agentmesh.runtime.outcome-reconciled` Outbox event, and stores the idempotency result. It never
holds a provider call inside a transaction because it never calls a provider at all.

Exact replay returns the existing resolution. A different request using the same key, conflicting
evidence, stale ownership, or a concurrently settled execution fails closed. Competing operators
therefore have one committed winner.

Confirmed success at or after the UTC budget deadline leaves Runtime, Run, and Attempt succeeded
but places the Task in `WAITING_APPROVAL` with candidate output. Parking already settled budget and
released quota, so reconciliation does not repeat those operations. A confirmed cancellation only
maps Task/Run/Attempt to canceled when a persisted cancel intent exists; otherwise the Runtime is
canceled and the business objects fail with `runtime.unrequested_cancellation`.

## Rollback boundary

The writer uses reader/schema compatibility delivered by A4.1b.2a. Once new observation or
resolution values have been written, migration 0048 is the schema floor. Operators may disable the
writer gate and roll the application back to the compatibility release, but must not downgrade to
0047. This slice does not provide reviewed/coordinated cutover, generic subprocess authority, or
production durable reattach.
