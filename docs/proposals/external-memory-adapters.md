# External Memory adapters

Status: private MemOS Cloud pilot implemented behind `external_memory=true` and
`AGENTMESH_MEMOS_REMOTE_EGRESS_ENABLED=true`; production backend registry, Credential
Broker integration, automatic reconciliation, and dashboards remain open.

## First integration decision (2026-09-30)

Keep the built-in reviewed PostgreSQL memory as the default. For the first real
external-provider acceptance, target **MemOS Cloud** with a dedicated project and
API key, using synthetic memories only. Its documented hosted API has add, search,
and delete operations and a China-hosted endpoint; this is a practical trial from
the current China-mainland server. Do not treat this decision as permission to send
customer data to a third party. External egress requires an explicit administrator
opt-in and data-processing review.

| Option | Fit | Additional requirement | First-release decision |
| --- | --- | --- | --- |
| PostgreSQL exact | Safe default and system of record | None | Keep enabled |
| pgvector | Local semantic index, no memory vendor egress | Extension and an embedding model/provider | Defer until embedding contract exists |
| MemOS Cloud | Managed add/search/delete API; fast live trial | Separate MemOS project/key and egress consent | First external adapter |
| Mem0 Platform | Managed add/search/delete API | Separate Mem0 project/key and egress consent | Second adapter; review free-plan data terms first |
| Self-hosted MemOS | Full control over memory service | Additional API, vector, and graph services | Not on the current small preview host |

The DeepSeek chat API key does **not** by itself provide an embedding model for
pgvector. The hosted memory trial therefore needs a separate memory-provider key.
Keep it out of chat, Git, agent records, and public HTTP. The private Console will
need a secure credential input before live provider acceptance.

### First adapter acceptance contract

1. Add a provider-neutral external-memory port and a MemOS Cloud adapter with
   bounded TLS requests and mocked contract tests. The private pilot uses a
   server-local secret environment variable; production must use the Credential
   Broker rather than exposing a key through Agent or Task configuration.
2. Add an administrator-owned backend registration and explicit remote-egress
   acknowledgement, disabled by default. Do not accept arbitrary endpoints or
   credentials from a Task or Agent.
3. Only mirror an accepted, eligible canonical record. Store an external ID mapping;
   keep acceptance, authorization, provenance, and expiry in PostgreSQL.
4. On search, authorize candidates locally before considering provider ranking.
   Intersect external hits with that set; never admit a provider-only record.
5. On revoke/delete/expiry, stop local retrieval immediately, enqueue provider
   deletion, retry failures, and expose reconciliation health. A provider outage
   falls back to `postgres-exact` without failing Task execution.
6. Exercise add, search, revocation, outage fallback, and data-egress visibility in
   the private preview using synthetic data before any real workspace is opted in.

### Current private-pilot limits

- The operator must set an exact `AGENTMESH_MEMOS_COMPANY_ID`, the external-memory
  feature gate, an egress flag, and a server-local `AGENTMESH_MEMOS_API_KEY` on the
  API and Worker. Defaults are off. Only this Company can use the remote ranker.
- An administrator must explicitly click **Sync approved notes to MemOS** over
  HTTPS or an SSH tunnel. Only up to 25 locally accepted, unexpired, policy-readable
  PUBLIC/INTERNAL records are mirrored. Sync replaces the dedicated remote user
  namespace; it does not alter PostgreSQL. MemOS may extract multiple fragments
  per note; the adapter uses the conversation ID to map hits back to canonical IDs.
- On retrieval, the current local policy filters records first. MemOS IDs that are
  not in that local set are discarded. If MemOS is unavailable, exact local ranking
  is used and the Task continues.
- A revoked record becomes ineligible locally immediately, but its remote copy can
  remain until the administrator syncs again. Do not use this pilot for regulated
  or sensitive data. Durable automatic deletion/reconciliation is still required
  before general availability.

## Decision

AgentMesh does not require a separate Memory product. PostgreSQL is the canonical source for
Memory content, status, namespace authorization, provenance, evidence, review, expiry, and
retrieval audit. This keeps the minimal deployment runnable without another service or API key.

Users may choose an optional recall/ranking backend when semantic retrieval quality or scale
requires it. Initial adapter targets are Mem0, MemOS, and local `pgvector`. These are accelerators,
not systems of record.

## Trust boundary

The application filters Company, Policy, namespace, type, lifecycle, expiry, and sensitivity before
calling `MemoryRankingBackend.rank(query, authorized_candidates)`. The backend must return exactly
the same candidate IDs in a preferred order. AgentMesh rejects a result that injects, removes, or
duplicates candidates.

For a remote backend, candidate content leaves the AgentMesh deployment. Enabling it therefore
requires:

- an explicit operator choice and data-egress acknowledgement;
- a credential reference resolved by the Credential Broker, never a raw key in Agent records;
- TLS, timeout, circuit breaker, bounded payloads, and health reporting;
- sensitivity rules that can prohibit remote ranking;
- deterministic fallback to `postgres-exact`;
- deletion/tombstone and re-index reconciliation;
- backend/version metadata on retrieval evidence.

An external backend must never decide whether Memory is accepted, who may read it, or whether a
remembered statement overrides a Business Object, Artifact, approval, or financial ledger.

## Configuration direction

Selection should be made per Memory Policy from an administrator-managed backend registry:

```text
MemoryBackend
- key
- kind: postgres-exact | pgvector | mem0 | memos
- endpoint
- credential_ref
- remote_egress_enabled
- allowed_sensitivity_levels
- health
- config_version
```

`MemoryPolicy` will reference a backend key and a fallback key. Individual Agents should not submit
arbitrary endpoints or API keys. This still gives users freedom to choose a backend while keeping
company governance centralized.

## Delivery slices

1. Ship `postgres-exact` and the candidate-set-preserving ranking interface. **Implemented.**
2. Add local `pgvector` hybrid ranking and evaluation fixtures.
3. Add the backend registry, credential references, health/fallback, and retrieval metadata.
4. Add opt-in Mem0 and MemOS adapters after contract tests against their supported APIs.
5. Add reconciliation, deletion, cost, latency, and retrieval-quality dashboards.

## Acceptance criteria

- AgentMesh works with no external Memory service and no Memory API key.
- Switching a ranker never changes namespace authorization or lifecycle decisions.
- Remote ranking is impossible without an explicit egress-enabled backend configuration.
- A backend outage falls back without losing canonical Memory or Task execution.
- Deletion and revocation remove future retrieval eligibility immediately in AgentMesh.
