# Controlled A2A Agent Card discovery

Status: Implemented baseline
Last updated: 2026-07-21

## Scope

This increment fetches A2A 1.0 Agent Cards from the discovery URL explicitly registered on a
tenant-scoped Peer. It deliberately separates discovering remote claims from trusting those claims.
The fetch creates an immutable candidate snapshot; only a separate operator command can make that
snapshot active for delegation.

## Security and trust boundary

- The URL must be clean HTTPS at `/.well-known/agent-card.json` without credentials, query or
  fragment.
- DNS must resolve exclusively to public addresses. The selected address is pinned to a TLS
  connection that verifies the registered hostname with TLS 1.2 or newer.
- Redirects are rejected. Request headers and response bodies are bounded, and only JSON media
  types are accepted.
- Endpoint hosts and protocol bindings declared by the fetched Card must still match the Peer
  allowlists. A Card cannot enlarge its own egress boundary.
- Discovery sends no credential. Authenticated or extended-card discovery remains deferred.
- Discovered Skills remain declared candidates; neither discovery nor activation turns them into
  verified platform capabilities.

## Cache and immutable evidence

The client sends the latest discovered ETag in `If-None-Match`. A successful response stores the
returned ETag and clamps `Cache-Control: max-age` to 60-86400 seconds, using
`AGENTMESH_A2A_DISCOVERY_DEFAULT_TTL_SECONDS` when the server provides no usable value. A `304 Not
Modified` response creates a new immutable snapshot from the previous body with a new observation
time and TTL. It never mutates or silently extends old evidence.

Each snapshot records `source=DISCOVERED` and the exact registered `source_url`. Manual imports keep
`source=MANUAL`. Both discovery and activation use idempotency records and emit domain events.

## API flow

1. Register a Peer and its standard discovery URL.
2. Call `POST /api/v1/a2a/peers/{peer_id}/agent-cards:discover` with `Idempotency-Key`.
3. Inspect the candidate digest, interfaces, security schemes, expiry and Skill claims.
4. Call `POST /api/v1/a2a/peers/{peer_id}/agent-cards/{snapshot_id}:activate` with a new
   `Idempotency-Key`.

Activation rejects expired snapshots and snapshots owned by another Peer or tenant. A failed fetch
does not change the active Card. Automatic refresh scheduling, signature verification, Card-change
approval policy and Peer health/circuit controls remain future increments.
