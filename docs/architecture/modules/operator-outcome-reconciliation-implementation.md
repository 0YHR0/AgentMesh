# Audited operator outcome reconciliation

Status: Implemented baseline
Last updated: 2026-07-21

## Scope

This increment closes external operations that deliberately stopped at `OUTCOME_UNKNOWN` without
replaying them. It covers governed MCP idempotent writes and initial outbound A2A Send Message
requests. Every command is tenant-scoped, RBAC-protected, idempotent and recorded as an immutable
`TaskResolution` plus a transactional Outbox event.

Enable `outcome_reconciliation` together with its `identity_rbac` and `human_resolution`
dependencies. Protocol-specific Gates still apply: MCP reconciliation additionally requires
`mcp_write_tools`; A2A reconciliation requires `a2a_delegation`.

## Evidence contract

An operator must provide a bounded reason, an external evidence reference and a lowercase SHA-256
digest of the evidence. AgentMesh stores the reference and digest, not the external evidence body.
The `outcome:reconcile` permission is granted to Operators and Federation Operators; each endpoint
also requires its protocol-specific read/operate permissions, so those roles cannot cross protocol
boundaries accidentally.

The same `Idempotency-Key` and command return the original resolution. A changed command with the
same key conflicts. Once the target leaves `OUTCOME_UNKNOWN`, a different key cannot overwrite the
conclusion.

## MCP convergence

`POST /api/v1/mcp/invocations/{invocation_id}/reconcile-outcome` accepts `SUCCEEDED` or `FAILED`.
Confirmed success requires a result digest and byte count; confirmed failure can include a safe
error summary. The linked `ToolInvocation` and `ToolExecutionAuthorization` converge in one
transaction. No Tool request is sent.

The containing Task is not changed. A confirmed external side effect does not prove that the Agent
workflow produced a valid Task result, so a Task that already failed remains failed. The operator
conclusion is available through the Task resolution ledger and Tool audit view.

## A2A convergence

`POST /api/v1/a2a/delegations/{correlation_id}/reconcile-outcome` supports two evidence-backed
decisions for an initial Send Message whose delivery was ambiguous and which has no remote Task ID:

- `REMOTE_TASK_BOUND` attaches a discovered remote Task ID, returns the correlation to
  `WAITING_REMOTE`, and schedules the existing Get Task reconciler immediately.
- `NOT_DELIVERED` records confirmed non-delivery and fails the correlation, Task and Run without
  repeating Send Message.

An operator assertion is never normalized as though it were an A2A protocol response. The durable
resolution identifies its human source; subsequent remote state still arrives through the normal
A2A adapter and convergence state machine.

## Deliberate limits

This baseline does not call application-specific MCP operation-status Tools, collect evidence
automatically, compensate side effects, accept bulk decisions, or recover an A2A remote Task ID by
guessing/list scans. Those capabilities require separate trust and policy contracts.
