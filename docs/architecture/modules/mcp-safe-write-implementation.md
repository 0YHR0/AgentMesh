# Governed MCP safe write runtime

Status: Implemented baseline.

Feature Gates: `mcp_read_tools`, `identity_rbac`, `policy_approval`, `governed_mcp`, and the separate
explicit opt-in `mcp_write_tools` Gate. Authenticated Servers additionally require
`credential_broker`.

## 1. Supported boundary

This increment executes only published `IDEMPOTENT_WRITE` Tool snapshots over MCP Streamable HTTP.
`NON_IDEMPOTENT_WRITE`, `IRREVERSIBLE`, managed stdio writes, dynamic Tool selection, and arbitrary
retry remain fail-closed. The published Registry classification is authoritative. MCP
`readOnlyHint` and `idempotentHint` are untrusted hints and are used only to detect a live contract
that has weakened since publication.

The protocol specification defines `idempotentHint` as repeated calls with the same arguments
having no additional effect, but explicitly says Tool annotations are hints. AgentMesh therefore
requires the reviewed input schema itself to contain a required string `idempotency_key`; the key
travels in normal Tool arguments so the remote service can enforce deduplication.

References: [MCP Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[MCP schema](https://modelcontextprotocol.io/specification/2025-11-25/schema), and
[Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

## 2. Approval and admission

1. A caller submits the exact logical Tool key and arguments to
   `POST /api/v1/mcp/tool-execution-intents`.
2. Catalog resolution pins Server, immutable Server Version, configuration digest, protocol Tool
   name, side-effect class, schema digest, full arguments digest, and idempotency-key digest into a
   canonical `mcp.tool.invoke` ActionIntent.
3. An independent approver issues the existing one-time Permit.
4. Task creation presents `Execution-Permit-Id`. AgentMesh consumes it before admission and writes
   a `ToolExecutionAuthorization` bound to that Task in the same transaction as the Task.

The Permit is intentionally consumed before the Task transaction. If Task persistence fails, the
Permit remains spent and the caller must request a new approval; this conservative failure mode
cannot accidentally execute an unapproved write.

## 3. Execution and delivery uncertainty

The Worker resolves the Catalog snapshot again and atomically creates `ToolInvocation` plus claims
the task authorization. Exact Server/Version/configuration/Tool/schema/arguments/key drift rejects
the call before network I/O. A claimed authorization can back only one invocation.

The Streamable HTTP adapter sends the stable key in the ordinary Tool arguments and its digest in
MCP request metadata. If the connection fails while `tools/call` may already have reached the
server, AgentMesh retries once using the same arguments and key. A confirmed Tool error becomes
`FAILED`; two ambiguous deliveries become terminal `OUTCOME_UNKNOWN`. Success, failure, and unknown
outcomes settle both the invocation and authorization records. The platform never creates a fresh
key or silently starts a second invocation. If a Worker restarts after the authorization was
claimed but before an outcome was persisted, recovery converts that in-flight record to
`OUTCOME_UNKNOWN` and stops instead of issuing another network call.

## 4. Audit and deferred reconciliation

`GET /api/v1/tasks/{task-id}/tool-invocations` exposes both the optional task authorization and its
invocations, including status and digests but not raw arguments, credentials, or response payloads.
PostgreSQL retains the consumed governed-action ID, requester, pinned target, argument and key
digests, invocation linkage, and terminal status.

This baseline makes ambiguous outcomes visible and prevents unsafe automatic replay. Operator
commands that query an application-specific operation-status Tool are deliberately deferred.
Operators can now reconcile `OUTCOME_UNKNOWN` from independently collected evidence without
replaying the call. Automatic evidence collection and broader write classes that need compensation
or explicit commit protocols remain deferred.
