# External Memory adapters

Status: proposed adapters; the safe ranking boundary and built-in `postgres-exact` implementation
are available.

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
