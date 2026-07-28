# MCP Tool Catalog and Agent Builder implementation

Status: Implemented baseline

## Outcome

The Console exposes published MCP Tool snapshots as a searchable Tool Catalog and lets Agent
authors select read-only logical Tool keys while creating an immutable Agent Version. The picker
does not grant runtime access by itself: the Worker still resolves the key through the published
tenant Catalog and enforces the Agent Version allowlist and call budget.

When `governed_mcp` is enabled, authorized Tool Providers can search the fixed official MCP
Registry endpoint from the Console. AgentMesh normalizes each candidate and accepts only clean
HTTPS Streamable HTTP endpoints. Candidates requiring arbitrary headers are marked incompatible;
Bearer candidates are shown but cannot use anonymous one-click discovery.

## Safe import flow

1. Search returns bounded candidate metadata; it does not install or execute package entries.
2. The operator confirms the runtime server name, endpoint, version, and owner.
3. A bounded discovery preview uses public-address DNS pinning, verified TLS, no redirects or
   proxies, response limits, protocol matching, and exact initialized server-name matching.
4. Only Tools whose live MCP annotation explicitly declares `readOnlyHint=true` are selectable.
5. Selected schemas are copied into a draft immutable Server Version and published through the
   existing Registry state machine.
6. The Agent Builder lists only active, published Tool bindings and records selected keys in the
   immutable Agent Version `tool_profile`.

Registry provenance is stored in the Server Version configuration digest. Registry listing is a
discovery source, not a trust assertion.

## Current boundary

- Arbitrary local stdio packages remain disabled; managed stdio is still the bundled confined
  workspace server.
- Authenticated discovery, OAuth, custom headers, package installation, and Docker MCP Gateway
  lifecycle management are deferred.
- One-click import is read-only. Write-class Tool publication continues through exact Policy
  approval and is not inferred from remote metadata.
- Model-originated Tool execution still requires `model_tool_loop`.

## Verification

Unit tests cover official Registry parsing, Bearer compatibility, dirty URL and custom-header
rejection, bounded error redaction, discovery metadata persistence, Console asset exposure, and
the existing MCP Registry and Streamable HTTP suites.
