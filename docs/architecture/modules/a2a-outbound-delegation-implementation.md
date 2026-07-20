# Governed outbound A2A delegation baseline

Status: Implemented baseline.

## 1. Delivered boundary

This increment lets an authenticated federation operator delegate a `FEDERATED` Task to one
trusted A2A 1.0 Peer. It binds the request to an immutable Agent Card snapshot and compatible
`HTTP+JSON` interface, requires an exact one-time Policy Permit, sends once, and projects the
remote Task or direct Message into the local Task/Run ledger.

The baseline supports `POST /message:send` with `returnImmediately=true` and explicit polling via
`GET /tasks/{id}`. Streaming, push notifications, remote cancellation, automatic polling,
and authentication schemes beyond the optional workload-bound HTTP Bearer path are intentionally
deferred.

## 2. Durable correlation and state convergence

`RemoteTaskCorrelation` is the protocol anti-corruption record. It stores the local Task and Run,
Peer and Card snapshot, endpoint and protocol version, deterministic outbound Message ID, request
digest, remote Task/context IDs, normalized state, response digest, bounded inline result, and
timestamps. Raw protocol responses and credentials are never persisted.

Delegation commits the local Run, `WAITING_REMOTE` Task, idempotency result, Outbox event, and
`PREPARED` correlation before network I/O. A second transaction marks `SENDING`. If a timeout can
have occurred after bytes were sent, the correlation becomes `OUTCOME_UNKNOWN`; AgentMesh does
not resend and risk duplicate remote work. Failures known to occur before delivery fail the local
Task and Run. Active, intervention, completed, failed, rejected, and canceled remote states are
normalized while late terminal results never overwrite an already terminal local Task.

## 3. Network and trust controls

- Only active, unexpired Card snapshots with A2A 1.0 `HTTP+JSON` interfaces are eligible.
- Cards declaring one supported HTTP Bearer requirement can use an exact governed
  CredentialBinding when `credential_broker` is enabled. Other requirements are rejected; no user
  Bearer token or ambient credential is forwarded.
- The client requires HTTPS, rejects URL credentials/query/fragment and redirects, resolves DNS
  before connection, rejects the endpoint if any answer is non-public, pins the selected public IP,
  and retains TLS hostname verification against the original host.
- Request and response bodies are bounded. Only inline `text` and `data` Parts are admitted to the
  local result; URL/raw Parts and malformed or oversized results require operator intervention.

## 4. API, authorization, and rollout

The explicit `a2a_delegation` Gate depends on `a2a_federation`, `identity_rbac`, and
`policy_approval`; it is disabled in every built-in profile. `FEDERATION_OPERATOR` has the
`a2a:delegate` permission. Operators first retrieve an exact delegation intent, request and obtain
Policy approval, then call the delegation endpoint with `Idempotency-Key` and
`Execution-Permit-Id`. Correlations can be listed, inspected, and explicitly reconciled.

Configuration bounds are `AGENTMESH_A2A_TIMEOUT_SECONDS`, `AGENTMESH_A2A_MAX_REQUEST_BYTES`,
`AGENTMESH_A2A_MAX_RESPONSE_BYTES`, and `AGENTMESH_A2A_MAX_INLINE_RESULT_BYTES`.

## 5. Next boundary

The workload identity, `SecretReference`, and Credential Broker boundary is now implemented in the
[Workload Credential Broker baseline](workload-credential-broker-implementation.md). The next
federation increment can add an authorized reconciliation scheduler, cancellation, controlled
discovery fetching, streaming/push delivery, and richer Artifact transfer after their replay and
trust contracts are explicit.
