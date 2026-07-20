# Workload Credential Broker baseline

Status: Implemented baseline.

Feature Gate: `credential_broker` (explicit opt-in; depends on `persistent_identity`,
`policy_approval`, and `a2a_federation`). Authenticated outbound use also requires
`a2a_delegation`.

## 1. Scope

This increment adds the first least-privilege credential path for outbound A2A calls. AgentMesh
stores only references, exact workload bindings, and lease audit metadata. It never accepts a
secret value through the Control API, places one in a protocol Message, or persists one in
PostgreSQL, Outbox, correlation, Task, Run, or Agent state.

The supported provider is the API process environment. The supported protocol scheme is one A2A
1.0 HTTP Bearer requirement. User bearer passthrough, API keys, Basic authentication, OAuth token
exchange, cloud secret managers, rotation orchestration, and mTLS are outside this baseline and
fail closed.

## 2. Durable model

- `SecretReference` identifies an environment key, purpose, version selector, and allowed HTTPS
  audiences. It contains no secret value.
- `CredentialBinding` binds one active tenant `SERVICE` Principal to one trusted Peer, immutable
  Agent Card snapshot/digest, declared security scheme, exact audience/scopes, runtime environment,
  SecretReference, and expiry.
- `CredentialLease` records request, issue, use, or failure metadata for one Task/Run operation. It
  contains no credential material and expires after 1 to 300 seconds.
- `RemoteTaskCorrelation` records only binding, scheme, scopes, and the latest lease identifier so
  operators can audit which authorization path was used.

Revocation is monotonic. Existing metadata remains available for audit, while new acquisition from
a revoked reference or binding is rejected.

## 3. Governed flow

1. A tenant administrator creates a metadata-only SecretReference.
2. AgentMesh reads the active Peer/Card and produces a canonical binding ActionIntent.
3. An independent approver approves `credential.binding.create`; the requester consumes the exact
   one-time Permit to create the binding.
4. An A2A delegation Permit includes the binding ID, Card identity, Bearer scheme, audience, and
   scopes. Any drift invalidates the Permit.
5. Immediately before each send or poll, the Broker locks and revalidates the binding, workload,
   Peer/Card contract, runtime environment, audience, scopes, expiry, and reference status.
6. It persists a `REQUESTED` lease, resolves the environment value outside the database
   transaction, marks the lease `ISSUED`, and returns redacted in-memory material.
7. The pinned HTTPS A2A adapter validates the material and adds `Authorization: Bearer ...` only to
   the target request. The lease is then settled to `USED`; provider failure becomes `FAILED` and
   prevents network delivery.

A fresh lease is acquired for every explicit poll. Credential acquisition failure never falls
back to an unauthenticated request.

## 4. API and authorization

`/api/v1/credentials` provides bounded list/create/revoke endpoints for SecretReferences and
CredentialBindings, a binding-intent endpoint, and read-only lease inspection. `CREDENTIAL_MANAGE`
is held by tenant administrators; auditors receive `CREDENTIAL_READ`. Response schemas expose no
secret material.

Enabling the Gate requires `AGENTMESH_CREDENTIAL_WORKLOAD_PRINCIPAL_ID` to be an active UUID-backed
tenant `SERVICE` Principal. `AGENTMESH_CREDENTIAL_LEASE_TTL_SECONDS` defaults to 60 and is bounded
to 1-300 seconds. Referenced environment variables must be supplied to the API process separately;
they are deliberately not generic fields in Compose or `.env.example`.

## 5. Verification and next boundary

Unit tests cover domain transitions, redaction, provider failure, revocation, Feature dependencies,
and fresh leases across send/poll. PostgreSQL integration verifies the governed authenticated A2A
round trip and proves the sentinel credential is absent from all credential rows. Migration tests
exercise upgrade/downgrade and schema drift checks.

Next increments can add provider adapters behind the existing port, OAuth client-credential
exchange with audience-bound caching, and MCP Streamable HTTP consumption. They must preserve the
same no-value persistence rule and cannot broaden a binding without a new governed intent.
