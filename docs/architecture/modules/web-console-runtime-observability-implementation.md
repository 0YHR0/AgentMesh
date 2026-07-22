# Web Console runtime observability implementation

Status: Implemented baseline

## Outcome

The zero-build Console now connects task operations to the immutable Agent execution contract. It
provides a feature-aware Agent Registry view and a Task-scoped governed MCP audit timeline without
adding a separate frontend service or weakening server-side authorization.

## Agent Registry view

When `agent_registry_management` is enabled, the Console exposes an `Agents` workspace backed by
the bounded `/api/v1/agents` query. It displays:

- Definition lifecycle, visibility, tags, default version, and version count;
- immutable version status, semantic version, content digest, role, and verified capabilities;
- canonical `model_policy`, including provider/model, reasoning effort, and output Token limit;
- canonical `tool_profile`, including logical Tool allowlist and maximum calls; and
- instructions and policy JSON behind an explicit details disclosure.

The task composer uses the loaded Registry names as Agent ID suggestions while preserving manual
entry for deployments whose Registry view is unavailable. The Console does not publish or mutate
Agent Versions in this slice.

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

- Agent Definition/Version creation, review, approval, and publishing forms;
- realtime SSE in place of polling;
- approval inbox, Artifact browser, and cross-domain audit timeline;
- scalable DAG layout, saved filters, and pagination controls.

## Verification

Static asset/API contract tests cover the feature-aware navigation, Agent policy view, and Tool
audit elements. The local browser smoke test verifies both minimal-profile behavior and the
standard/full Agent workspace when the relevant Gates are enabled.
