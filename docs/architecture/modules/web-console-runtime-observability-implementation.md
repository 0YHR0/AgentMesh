# Web Console Agent operations and runtime observability

Status: Implemented baseline

## Outcome

The zero-build Console now connects task operations to the immutable Agent execution contract. It
provides feature-aware Agent authoring and Registry views plus a Task-scoped governed MCP audit
timeline without adding a separate frontend service or weakening server-side authorization.

## Agent Registry view

When `agent_registry_management` is enabled, the Console exposes an `Agents` workspace backed by
the bounded `/api/v1/agents` query. It displays:

- Definition lifecycle, visibility, tags, default version, and version count;
- immutable version status, semantic version, content digest, role, and verified capabilities;
- canonical `model_policy`, including provider/model, reasoning effort, and output Token limit;
- canonical `tool_profile`, including logical Tool allowlist and maximum calls; and
- instructions and policy JSON behind an explicit details disclosure.

The task composer uses the loaded Registry names as Agent ID suggestions while preserving manual
entry for deployments whose Registry view is unavailable.

## Governed authoring lifecycle

Operators with the existing Registry permissions can complete the baseline lifecycle from the
Console:

1. create an Agent Definition with owner, visibility, description, and tags;
2. create an immutable Version draft with role, instructions, declared capabilities, canonical
   model policy, optional credential reference, and bounded Tool profile;
3. submit a `DRAFT` Version for review; and
4. publish an `IN_REVIEW` Version with verified capabilities and optionally make it the Definition
   default.

The browser sends policy metadata only. Provider secrets are never accepted. Domain validation
remains authoritative for provider fields, Token limits, Tool allowlists, call budgets, SemVer,
capabilities, and lifecycle transitions.

When `policy_approval` is enabled, the publish dialog requires the operator to supply the exact
one-time `Execution-Permit-Id`; the API consumes and verifies that Permit against the final publish
arguments. The Console does not manufacture or bypass Permits. When Policy Approval is disabled,
the same endpoint remains protected by Registry state and RBAC permissions.

The same Gate enables a Policy workspace and an exact-intent request action in the publish dialog.
Approvers can inspect canonical arguments, action hashes, expiry and decision history before
recording a reasoned approval or rejection. Unconsumed Permits can be copied for the original
requester; server-side identity, permission checks and separation of duties stay authoritative.

## Artifact catalog and Run lineage

When `artifact_service` is enabled, operators can create safe inline text/JSON Artifacts, append
immutable Versions, inspect integrity and scan metadata, and preview or download content through
authenticated API requests. Task detail projects Artifact Versions whose `producer_run_id` belongs
to one of the Task's persisted Runs, providing direct evidence lineage without guessing from output
text.

## Tool audit timeline

When `mcp_read_tools` is enabled, selecting a Task queries its authoritative
`/api/v1/tasks/{task_id}/tool-invocations` ledger. The timeline shows status, logical Tool key,
server, side-effect class, invocation/schema identifiers, start age, and sanitized failure text.
It does not reconstruct calls from model prose or expose raw arguments, results, or credentials.

## Feature and security behavior

- `/api/v1/features` drives navigation and optional queries; a disabled Registry does not produce
  background 403 noise in the minimal profile.
- Identity/RBAC remains enforced by the existing API. The optional Bearer token remains in
  `sessionStorage` and is sent only to same-origin Control API requests.
- Content remains escaped before HTML insertion, and the existing same-origin Content Security
  Policy is unchanged.
- Polling refreshes only the active workspace and reuses bounded list endpoints.

## Deferred

- realtime SSE in place of polling;
- cross-domain audit timeline;
- scalable DAG layout, saved filters, and pagination controls.

## Verification

Static asset/API contract tests cover feature-aware navigation, lifecycle forms, Permit forwarding,
the Agent policy view, and Tool audit elements. The local browser smoke test verifies both
minimal-profile behavior and the complete Definition/draft/review/publish lifecycle in the full
Agent workspace when the relevant Gates are enabled.
