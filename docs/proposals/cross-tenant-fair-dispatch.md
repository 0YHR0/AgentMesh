# Cross-tenant fair dispatch

Status: Deferred proposal

Target release: Post-baseline; explicitly out of scope for the current single-team release.

## Problem

The current hard quota layer prevents a tenant or project from exceeding configured concurrent
Attempt limits, but it does not choose fairly between eligible work from different tenants. At
larger scale, a high-volume tenant could dominate queue order even when every individual admission
decision is valid.

## Proposed boundary

A future dispatcher may add:

- deterministic weighted fair selection using the existing versioned quota-policy weight;
- deadline aging so old or time-sensitive work cannot starve;
- a small reserved recovery lane for expired leases, resume commands, cancellation convergence,
  and other system-owned recovery work;
- durable scheduling decisions and explanations in PostgreSQL; and
- bounded dispatch batches using `SKIP LOCKED` for horizontally scaled dispatchers.

The dispatcher must not turn Redis into the scheduling source of truth, bypass hard quota
admission, or reorder work invisibly. The selected policy version, score inputs, lane, and reason
must be inspectable.

## Non-goals

- This proposal does not change the current single-team execution path.
- It does not introduce billing tiers or product-plan semantics.
- It does not make deadlines a guarantee.
- It does not replace Worker leases, Outbox delivery, or quota reservations.

## Revisit trigger

Implement only when AgentMesh operates multiple active tenants or projects on a shared Worker pool
and queue-age evidence shows contention that hard quotas alone cannot control.
