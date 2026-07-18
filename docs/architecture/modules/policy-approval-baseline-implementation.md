# Policy Decision and Approval baseline implementation

Status: Partial

Feature Gate: `policy_approval` (depends on `identity_rbac`; explicit opt-in)

## Responsibility

This increment adds a durable, fail-closed governance loop for high-risk Control API actions. A
requester creates a canonical ActionIntent, the built-in versioned Policy returns `ALLOW`, `DENY`,
or `REQUIRE_APPROVAL`, and an independently authenticated Approver may create an append-only
decision. An allowed or approved action receives a short-lived, one-time Execution Permit.

Initial enforcement points are Agent Version publication and Task budget increase/resume.

## GovernedAction aggregate

`GovernedAction` stores immutable intent and decision inputs: tenant, requester, action/resource,
canonical arguments, canonicalization version, action hash, Policy bundle/version, result, reason
code and expiry. Approval/Permit lifecycle fields advance with an optimistic revision.

`ApprovalDecision` is append-only. The requester cannot decide its own Approval, including when it
also has administrative permissions. Exact duplicate decisions replay safely; conflicting decisions
fail. `APPROVER` is separate from `AGENT_AUTHOR` and `AGENT_PUBLISHER`.

## Canonical binding and Permit

Canonicalization version `agentmesh-action-v1` hashes trusted tenant/requester identity, action and
resource identity, and sorted JSON arguments. Agent capability lists are normalized as sets. The
execution endpoint recomputes this hash; a changed requester, resource, budget, capability set, or
publication option cannot reuse the Permit.

The Permit expires with the action and is consumed once under a row lock. `ALLOW` creates it
immediately, `DENY` never creates one, and `REQUIRE_APPROVAL` creates one only after approval.

## API

```text
POST /api/v1/policy/actions
GET  /api/v1/approvals?status=PENDING
POST /api/v1/approvals/{approval_id}/approve
POST /api/v1/approvals/{approval_id}/reject
```

The approved requester submits `Execution-Permit-Id` to the governed business endpoint. Policy
events and the business module's existing events use the transactional Outbox.

## Consistency boundary

This baseline conservatively consumes the Permit immediately before the existing business command
transaction. Therefore no side effect can occur without approval and a failed side effect cannot
reuse the Permit, but the operator must request a new intent after such a failure. A later increment
can move consumption into each business UoW and add outcome reconciliation without changing the
ActionIntent or Permit contract.

## Deliberate boundary

The first engine is a deterministic JSON action-to-result map with a named built-in bundle/version.
It does not yet provide OPA/Rego, signed bundle publication/rollback, conditions or obligations,
quorum/staged/delegated approvals, supersession, break-glass, WORM export, or Policy administration
UI. No write-capable MCP Tool exists yet, so Tool commit enforcement remains future work.
