# Trusted A2A Peer and Agent Card Registry baseline

Status: Implemented baseline.

## 1. Delivered boundary

This slice implements the discovery and trust catalog required before AgentMesh may delegate work
to an independent A2A server. It is intentionally not a network client: operators register a
tenant-scoped Peer and explicitly submit an A2A v1 Agent Card document. Remote discovery fetching,
credentials, task delegation, streaming, push callbacks, polling, and state convergence remain
separate increments.

The split prevents arbitrary discovery URLs from becoming an SSRF surface before a controlled
network policy and credential broker exist.

## 2. Domain and persistence

`A2APeer` owns the administrative trust boundary: owner, HTTPS discovery URL, allowed endpoint DNS
hosts, allowed protocol bindings, trust tier, lifecycle status, and the selected Card snapshot.
`AgentCardSnapshot` is immutable retrieval evidence containing the raw forward-compatible Card,
deterministic JSON SHA-256 storage digest, projected interfaces, declared Skill candidates, capabilities, security
scheme names, signature presence state, fetch time, expiry, and ETag.

Every successful refresh creates a new snapshot even when the Card digest is unchanged. This keeps
retrieval time and TTL truthful while stable digests reveal unchanged content. A replay using the
same Idempotency-Key returns the original snapshot. The active snapshot is a versioned Peer pointer;
suspension and revocation never delete history.

PostgreSQL stores Peers and snapshots in `a2a_peers` and `a2a_agent_card_snapshots`. Peer updates use
optimistic revisions and a row lock during import, so concurrent refreshes cannot lose the active
pointer. Outbox events and idempotency records commit in the same transaction.

## 3. Validation and trust rules

- The accepted protocol baseline is A2A 1.0; `protocolVersion` is read from every ordered
  `supportedInterfaces` entry.
- Interfaces require HTTPS, an allowed binding (`JSONRPC`, `HTTP+JSON`, or `GRPC`), and a host
  explicitly allowed by the Peer. URL credentials and fragments are rejected.
- Required A2A v1 identity, capabilities, media mode, interface, and Skill fields are validated;
  unknown fields remain in the raw Card for forward compatibility.
- Card size is capped at 256 KiB, TTL at 60 seconds through 24 hours, interfaces at 16, and Skills
  at 200.
- A Card signature is reported only as `UNSIGNED` or `PRESENT_UNVERIFIED`. This slice never claims
  cryptographic verification or RFC 8785/JCS signature canonicalization.
- Raw Cards are recursively rejected when fields conventionally used for credential values (for
  example token, password, client secret, or authorization) are present.
- Imported Skills are exposed as `DECLARED_CANDIDATE`; they do not become verified Agent Registry
  capabilities and cannot drive scheduling.
- Trust tier is an audited operator assertion, not proof of Card signature verification.
- Credentials and tokens are never accepted in Peer configuration or returned by the API.

## 4. API, authorization, and feature gate

The explicit-opt-in `a2a_federation` Gate depends on `identity_rbac` and is excluded from all built-in
profiles. `FEDERATION_OPERATOR` has `a2a-peer:read` and `a2a-peer:manage`; Operators and Auditors can
read, and Tenant Admins retain all permissions.

Endpoints under `/api/v1/a2a` register/list Peers, import Card snapshots, resolve the non-expired
active snapshot, suspend a Peer, and revoke its active Card. Mutation endpoints authenticate the
actor; create/import require an Idempotency-Key. Lists are bounded to 100 Peers and the newest 20
snapshots per Peer.

## 5. Failure semantics and next boundary

Resolution fails closed when the Peer is not active, the active pointer is absent, or the snapshot
has expired. Tenant mismatches are returned as not found. Endpoint host, binding, protocol, Card
shape, or size violations do not persist partial state.

The next A2A increment may consume only `resolve_active_card()` output to bind an immutable Card
snapshot and preferred compatible interface to an outbound correlation. It must add a controlled
HTTP client, workload credentials, outcome-unknown reconciliation, and remote Task correlation
before it can execute a real delegation.
