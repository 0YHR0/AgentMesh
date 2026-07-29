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

## Retrieval contract

Exact search performs:

1. Company and Policy scope checks;
2. requested namespace and Memory Type authorization;
3. accepted/valid/expiry and sensitivity filtering;
4. deterministic term/confidence/recency ranking;
5. count and approximate token-budget truncation;
6. conflict marking for unresolved memories of the same namespace/type;
7. immutable retrieval evidence.

Each `MemoryRetrieval` snapshots Policy ID/version, query digest, namespaces, requested types,
ordered result IDs, principal, reason, and optional Task/Run correlation. This lets a future Run
context assembler reproduce which Memory versions were available without storing hidden
reasoning.

## Security boundary

PostgreSQL stores full bounded content. Candidate inspection requires Company management
permission; normal retrieval must pass a Policy and produces an audit record. Domain events contain
content digests and hashed namespace IDs rather than content. Revoked, superseded, expired,
unauthorized, and forbidden-sensitivity records never enter results.

The current baseline is exact search and needs no embeddings or paid API. Automatic Run context
injection, retention/deletion workers, semantic `pgvector` ranking, model extraction, and the
Office Memory inspector remain later increments.

Enable with:

```dotenv
AGENTMESH_FEATURE_GATES=company_model=true,organizational_memory=true
```
