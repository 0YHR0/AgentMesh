# Controlled MCP capability snapshot refresh

Status: Implemented baseline.

Feature Gate: `governed_mcp`.

## 1. Scope

This increment lets an authenticated Tool Provider explicitly refresh a published, public MCP
Streamable HTTP Server Version. It performs `initialize` followed by paginated `tools/list` using
MCP `2025-11-25`, then stores immutable discovery evidence. Discovery never edits a published
Version, creates Tool capabilities, or broadens the runtime Catalog.

Authentication-required discovery, notification/background refresh, write execution,
Resources/Prompts, OAuth, and automatic publication remain deferred.

## 2. Trust and resource boundary

Discovery reuses the Streamable HTTP Gateway's clean HTTPS validation, all-answer public DNS
validation, pinned socket address, original-host TLS verification, TLS 1.2 minimum, disabled
redirects/proxy environment, response bounds, exact protocol negotiation, and Server identity
check. It additionally enforces:

- a configurable maximum Tool count (`AGENTMESH_MCP_DISCOVERY_MAX_TOOLS`, default 256);
- a cumulative metadata limit shared with `AGENTMESH_MCP_MAX_RESULT_BYTES` across all pages;
- JSON Schema 2020-12 validation, unique Tool names, and non-repeating pagination cursors;
- a 60-86400 second verification TTL (`AGENTMESH_MCP_DISCOVERY_TTL_SECONDS`, default 3600).

No SDK session ID, Tool body, credential, or raw schema is persisted. Snapshots retain normalized
Tool name, input Schema Digest, `readOnlyHint`, capability digest, target/version/configuration
identity, timestamps, actor, status, and safe failure category.

## 3. Compatibility and Catalog behavior

Snapshot status is deterministic:

- `COMPATIBLE`: every published Tool is present with the exact Schema Digest; published read-only
  Tools still declare `readOnlyHint=true`, and no extra Tool exists.
- `EXPANDED`: every published Tool remains compatible but the Server advertises additional Tools.
  Additions are evidence only and require a separately reviewed Version before becoming visible.
- `INCOMPATIBLE`: a published Tool is missing, its schema changed, or its read-only hint weakened.
- `FAILED`: bounded transport, protocol, identity, schema, pagination, or discovery failed.

The latest snapshot governs resolution. `INCOMPATIBLE`, `FAILED`, or expired evidence blocks the
published Version before invocation. A later compatible refresh restores resolution. Versions
without a snapshot retain the prior live-schema-check baseline for backward compatibility.

## 4. API and consistency

`POST /api/v1/mcp/server-versions/{version_id}/discovery-snapshots` performs an idempotent refresh;
`GET` on the same path returns bounded history. The command accepts only active, published,
unauthenticated Streamable HTTP targets. It revalidates immutable Version/configuration and Server
endpoint state after network I/O before committing the snapshot and Outbox audit event.

Authenticated targets fail closed until discovery can acquire a separately auditable, non-Task
credential lease rather than misusing an invocation-linked lease.

## 5. Verification and next boundary

Tests cover pagination, cursor cycles, Tool/byte limits, expansion without privilege widening,
schema drift, failed and expired snapshot blocking, idempotent replay, management API responses,
PostgreSQL JSONB round-trip, and Alembic downgrade/upgrade drift checks.

The next MCP increment is safe write execution: exact invocation approval, stable operation keys,
idempotent-only automatic retry, and durable `OUTCOME_UNKNOWN` reconciliation.
