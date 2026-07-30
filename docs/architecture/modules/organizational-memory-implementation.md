# Organizational Memory implementation

Status: implemented governed baseline.

## Outcome

The `organizational_memory` feature gives multiple digital employees one PostgreSQL-backed
long-term Memory Service while preserving exact namespace authorization, immutable provenance,
review, expiry, and retrieval evidence. It does not store raw conversations or replace Tasks,
Artifacts, Business Objects, policy, or financial systems of record.

## Policy and namespace contract

Versioned `MemoryPolicy` records define readable and writable namespace patterns, allowed and
automatically accepted Memory Types, forbidden sensitivity levels, retrieval and context bounds,
default TTL, review role, and whether extraction may run. A higher Policy version deactivates the
prior version without rewriting historical retrieval evidence.

Supported exact namespaces are Company, Organization Unit, Project, Position, Employee,
Relationship, and User. Authorization uses exact metadata and glob patterns before any content
ranking. A caller requesting one unauthorized namespace receives a denial rather than a partial
result.

## Write and lifecycle contract

A candidate contains bounded content, SHA-256 digest, durable provenance, one to twenty evidence
references, confidence basis points, sensitivity, validity, optional expiry, and optional
supersession target. Deterministic secret patterns reject likely credentials before persistence.
Duplicate active/candidate content in the same namespace and type is rejected.

The lifecycle is:

```text
CANDIDATE -> ACCEPTED -> SUPERSEDED | EXPIRED | REVOKED
          -> REJECTED
```

Review requires the Policy's declared role and an attributable reason. Accepting a correction
atomically marks the prior accepted Memory as superseded. Records, evidence, and review decisions
remain audit history. Automatic acceptance is available only for Memory Types explicitly declared
by Policy; model extraction remains off by default.

## Runtime context contract

Before an executor Run starts, `RuntimeMemoryService` resolves the Company, Position, active
Memory Policy, and permitted Company/Project/Unit/Position/Employee namespaces from the immutable
Task input. It retrieves bounded accepted records and adds them to
`work_item.input.agentmesh_memory`. Reviewer Runs intentionally receive no automatic Memory so
that review remains independent.

Memory is injected as scoped evidence, not prompt instructions. A retrieval failure is fail-open:
the Run continues without Memory and logs the failure. The immutable retrieval record still makes
successful injection attributable to its Task and Run.

When a Task completes, a workflow may return at most five structured `memory_candidates`:

```json
{
  "memory_candidates": [
    {
      "memory_type": "PATTERN",
      "content": "Evidence gaps should be assigned before drafting.",
      "namespace_type": "COMPANY",
      "namespace_id": "company-uuid",
      "confidence_basis_points": 6500,
      "sensitivity": "INTERNAL"
    }
  ]
}
```

Extraction must be enabled by the selected Policy. Candidates are validated by the same namespace,
type, sensitivity, secret, provenance, evidence, and deduplication rules as explicit writes.
Agent-generated confidence is capped at 7500. Candidate creation and Task completion share one
transaction, so a crash cannot commit one without the other. Unless the Policy explicitly permits
automatic acceptance for that Memory Type, the result remains `CANDIDATE` for human review.

## Retrieval and backend contract

Exact search performs:

1. Company and Policy scope checks;
2. requested namespace and Memory Type authorization;
3. accepted/valid/expiry and sensitivity filtering;
4. pluggable ranking over only the already-authorized canonical candidate set;
5. count and approximate token-budget truncation;
6. conflict marking for unresolved memories of the same namespace/type;
7. immutable retrieval evidence.

Each `MemoryRetrieval` snapshots Policy ID/version, query digest, namespaces, requested types,
ordered result IDs, principal, reason, and optional Task/Run correlation. This lets a future Run
context assembler reproduce which Memory versions were available without storing hidden
reasoning.

The built-in `postgres-exact` backend performs deterministic term/confidence/recency ranking and
requires no external service or API key. `MemoryRankingBackend` is a dependency-injection boundary
for optional semantic recall systems. An adapter cannot add or remove canonical candidates: the
service rejects any returned ID set that differs from the authorized set.

PostgreSQL remains the source of truth for content, lifecycle, permissions, provenance, and audit
even when an external system ranks candidates. Remote content egress therefore requires an
explicitly configured adapter and credential; it is never enabled by installing AgentMesh.

## Security boundary

PostgreSQL stores full bounded content. Candidate inspection requires Company management
permission; normal retrieval must pass a Policy and produces an audit record. Domain events contain
content digests and hashed namespace IDs rather than content. Revoked, superseded, expired,
unauthorized, and forbidden-sensitivity records never enter results.

The current baseline is exact search and needs no embeddings or paid API. Automatic Run context
injection, structured governed candidate capture, and the Admin/Office Memory inspector are
implemented. The inspector exposes the learning review queue, canonical ledger, active policies,
Task/Run retrieval trails, and per-employee recall/candidate counts. Retention/deletion workers,
free-form model extraction, and semantic `pgvector` or external ranking adapters remain later
increments.

Enable with:

```dotenv
AGENTMESH_FEATURE_GATES=company_model=true,organizational_memory=true
```
