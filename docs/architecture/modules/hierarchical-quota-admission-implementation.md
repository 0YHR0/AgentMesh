# Hierarchical quota admission implementation

Status: implemented baseline  
Feature Gate: `quota_admission` (depends on `identity_rbac`)  
Tracks: [Issue #69](https://github.com/0YHR0/AgentMesh/issues/69)

## Scope

This increment adds durable concurrent-Attempt limits at the tenant and project levels. Every Task
has a normalized `project_id` (`default` when omitted). Administrators publish immutable policy
versions with a hard concurrency limit and a scheduling weight. The weight is retained as the
contract for the later weighted dispatcher; it does not alter queue order in this increment.

Before an Attempt lease is committed, the Worker locks all applicable active policy rows in a
stable order, counts live reservations across every version of each scope, and creates one
reservation per applicable policy in the same PostgreSQL transaction as the Attempt. If either the
tenant or project limit is full, the lease transaction rolls back and the queued message remains
available for retry.

Reservations are released on success, failure, cancellation, pause finalization, coordinated
sibling cancellation, or lease expiry. A policy replacement never resets capacity: reservations
made against an older version continue to count against the same tenant/project scope until they
are released.

## API

- `PUT /api/v1/quotas/policies` publishes the next tenant or project policy version.
- `GET /api/v1/quotas/policies` lists active policies and their live reservation counts.
- `POST /api/v1/tasks` accepts `project_id`; existing clients use `default` unchanged.

Reads require `quota:read`; writes require `quota:manage`. The Feature Gate depends on RBAC so a
deployment cannot accidentally expose quota administration anonymously.

`project_id` is currently an accounting and scheduling label inside one authenticated tenant, not
a data-authorization boundary. Deployments should configure a tenant policy as the non-bypassable
ceiling. Project membership claims and project-scoped authorization remain part of the broader
multi-tenancy work.

## Safety invariants

- Tenant and project limits are conjunctive; a Task must have capacity in every configured scope.
- Policy-row locks serialize competing admissions, preventing concurrent oversubscription.
- Lowering a limit below current use drains naturally; it does not cancel active Attempts.
- Historical policy rows and reservations remain attributable to their creator and policy version.
- Feature-disabled deployments do not create new reservations, but terminal Attempt paths still
  release pre-existing reservations. This permits a safe disable after in-flight work drains.

## Deferred

- Cross-tenant weighted fair queueing, deadline aging, and the reserved recovery lane. The current
  Redis Worker consumes one configured tenant stream, so true WFQ requires a cross-tenant
  dispatcher rather than only a weight column.
- Platform, Agent, model, and Tool quota scopes; distributed reservation coordination for A2A.
- Policy retirement, history-list APIs, alerts, and admission-rejection metrics.

## Verification

Unit/API tests cover hierarchical enforcement, release, Feature Gate behavior, policy versioning,
and old-version capacity continuity. PostgreSQL integration coverage races two admissions against
a one-slot project policy and proves that exactly one transaction succeeds.
