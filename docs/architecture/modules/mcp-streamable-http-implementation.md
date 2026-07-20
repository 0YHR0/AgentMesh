# Governed MCP Streamable HTTP runtime

Status: Implemented baseline.

Feature Gates: `mcp_read_tools` and `governed_mcp`; authenticated Servers additionally require
`credential_broker`.

## 1. Scope

This increment executes published `READ_ONLY` Tool snapshots over MCP Streamable HTTP using the
official Python SDK and protocol version `2025-11-25`. The existing Catalog pins every invocation
to a tenant, Server, immutable Server Version, configuration digest, protocol Tool name, input
Schema Digest, and endpoint. Registered endpoints are not a discovery or trust shortcut.

Write Tools, Resources, Prompts, OAuth discovery/exchange, resumable sessions, automatic discovery
refresh, and server-side sampling or elicitation remain disabled.

## 2. Network and protocol boundary

- Endpoints must be clean HTTPS URLs without user information, query, fragment, or credential-like
  fields. Redirects, proxy environment variables, and non-HTTP transports are disabled.
- Every DNS answer is validated before connection; one private, loopback, link-local, multicast,
  reserved, or otherwise non-global answer rejects the invocation. The validated public address is
  pinned for the socket connection while TLS certificate and hostname verification use the
  original hostname, closing the DNS rebinding window.
- TLS requires at least version 1.2. Each response body and normalized Tool result is bounded by
  `AGENTMESH_MCP_MAX_RESULT_BYTES`; the whole operation is bounded by
  `AGENTMESH_MCP_HTTP_TIMEOUT_SECONDS` (1-300 seconds).
- Each connection performs `initialize` and Tool discovery. Runtime identity, exact protocol,
  `readOnlyHint`, input schema, configuration digest, and published snapshot must still match.

The first adapter uses one fresh SDK session per invocation. It deliberately does not persist or
resume `Mcp-Session-Id`, avoiding cross-tenant or cross-invocation session state.

## 3. Credential flow

An MCP credential binding is exact to the workload `SERVICE` Principal, Server, immutable Version,
configuration digest, SecretReference, Bearer scheme, audience, scopes, environment, and expiry.
Creation requires the canonical `mcp.credential-binding.create` ActionIntent and a one-time Permit.

Immediately before network I/O, the Credential Broker creates an invocation-scoped lease and
revalidates the full binding both before and after resolving the environment-backed secret. The
Bearer value exists only in memory and is injected inside the HTTP adapter. PostgreSQL stores only
binding and lease metadata. If a Server declares authentication required, a missing, revoked,
expired, or drifting binding fails closed and never falls back to an anonymous request.

## 4. API and audit

MCP Server registration accepts an immutable `authentication_required` flag for Streamable HTTP.
Credential APIs add MCP binding intent/create/list/revoke and lease inspection beside the A2A
contracts. Responses expose references and lifecycle metadata only—never credential values.

Every Tool call retains the existing `ToolInvocationRecord` audit. Authenticated calls also record
an invocation/task/run-scoped credential lease which is settled to `USED` or `FAILED` without
including the Authorization header, token, SDK session, request body, or Tool result.

## 5. Verification and next boundary

Tests cover endpoint validation, all-answer DNS rejection, pinned connections, redirect/proxy
exclusion, wire response bounds, routed stdio/HTTP execution, governed binding acquisition,
revocation, authenticated-downgrade prevention, PostgreSQL lease linkage, secret non-persistence,
and Alembic upgrade/downgrade drift checks.

Controlled discovery refresh is implemented by the linked
[capability snapshot increment](mcp-capability-refresh-implementation.md). The next MCP increment
should add safe write execution with idempotency, explicit commit policy, and unknown-outcome
reconciliation before enabling any write side-effect class.
