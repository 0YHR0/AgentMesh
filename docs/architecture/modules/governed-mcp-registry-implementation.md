# Governed MCP Registry and Catalog baseline implementation

Status: Partial

Feature Gate: `governed_mcp` (explicit opt-in; depends on `mcp_read_tools`, `identity_rbac`, and
`policy_approval`)

## Responsibility

This increment replaces the code-only Tool allowlist boundary with a durable, tenant-scoped MCP
Registry and default-deny Catalog. It records MCP Server ownership/transport, immutable published
Server Versions, exact configuration digests, and Tool Capability snapshots with JSON Schema
digests and explicit side-effect classes.

It deliberately does not make arbitrary registered endpoints executable. The only supported
runtime adapter remains the bundled confined workspace stdio Server; future Streamable HTTP and
credential adapters must implement the existing Gateway/Tool Catalog ports.

## Lifecycle

1. A `TOOL_PROVIDER` registers an MCP Server without credential values or arbitrary command text.
2. The provider creates a draft Version with protocol and configuration digest.
3. One or more Tool snapshots are added while draft. JSON Schema 2020-12 is validated immediately.
4. Publication freezes the Version and its Tool set and activates the Server.
5. Version revocation or Server suspension removes every affected Tool from Catalog resolution.

Published Version snapshots are immutable. Logical Tool keys must have exactly one active published
binding per tenant; ambiguity fails closed instead of choosing an arbitrary provider.

## Policy boundary

Side-effect classes are `READ_ONLY`, `IDEMPOTENT_WRITE`, `NON_IDEMPOTENT_WRITE`, and
`IRREVERSIBLE`. A read-only Version may publish directly after RBAC checks. If any Tool is a write
class, publication requires the existing Policy/Approval flow and one-time Permit.

The canonical ActionIntent binds the requester, Version ID, configuration digest, and the sorted
set of logical key, protocol Tool name, Schema Digest, and side-effect class. Changing any field
requires a new approval. The publication transaction reloads and re-hashes the complete snapshot,
then serializes competing logical-key publications with tenant-scoped PostgreSQL advisory locks.
Permit consumption occurs conservatively before the Registry transaction;
a crash in that narrow window denies retry and requires a new approval rather than risking an
unapproved publication.

## Runtime boundary

With `governed_mcp` enabled, `ReadOnlyMcpAgentExecutor` resolves the requested logical key through
the Catalog. It receives a binding pinned to Server Version and Schema Digest. The Gateway compares
the live MCP discovery schema to that digest before invocation; drift blocks execution.

The bundled `workspace.read_text` Server/Version/Tool is seeded through the same Registry and
Catalog path. When the Gate is disabled, the previous static read-only binding remains available,
preserving existing `full` profile behavior.

## Administration and audit

The `/api/v1/mcp` administration APIs support bounded Server listing, registration, draft Version
and Tool creation, publication, revocation, and Server suspension. Writes use durable idempotency
where creation can be retried and emit Outbox audit events. `TOOL_PROVIDER` is separate from
`APPROVER`; self-approval remains prohibited by the Policy module.

## Deliberate boundary

Deferred work includes real write Tool execution, Agent ToolProfile bindings, dynamic managed
stdio launch, Streamable HTTP, OAuth/Credential Broker, discovery refresh and health checks,
Resources/Prompts, rate limits, circuit breaking, large-result Artifact conversion, and unknown
write-outcome reconciliation.
