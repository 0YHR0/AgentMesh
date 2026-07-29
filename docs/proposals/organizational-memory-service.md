# Organizational memory service

Status: Proposed

## Outcome

AgentMesh digital employees should retain useful, governed experience across Tasks without treating
raw conversation history as company knowledge. The Organizational Memory Service provides one
shared physical backend with logically isolated company, department, project, relationship,
employee, and user namespaces.

Memory augments repository, business-object, Artifact, and Task evidence. It never overrides those
systems of record.

## Memory layers

| Layer | Scope | Examples | Current or proposed owner |
|---|---|---|---|
| Working memory | Run/thread | Messages, pending Tool call, graph state | LangGraph checkpoint |
| Episodic evidence | Task | Plan, Handoff, result, review, failure | Task/Artifact ledgers |
| Semantic memory | Cross-Task | Stable facts, preferences, relationships | Memory Service |
| Procedural memory | Agent/Position | SOP, approved method, escalation rule | Agent Version + Memory |
| Organizational memory | Company/Department | Strategy, policy, operating lessons | Memory Service |

Checkpoint retention and long-term Memory retention are separate policies.

## Principles

### Shared backend, isolated namespaces

The default backend is PostgreSQL. `pgvector` may add semantic search, but exact filters and
permission checks remain authoritative. Deploying one database per Agent is not required.

### Candidate before accepted memory

An Agent proposes a candidate. Validation, deduplication, sensitivity classification, provenance
checks, and Memory Policy determine whether it becomes accepted.

### Append and supersede

Accepted memories are immutable. Corrections create a new record that supersedes the old record.
Historical Runs continue to reference the version they retrieved.

### Retrieval is evidence

Every memory injected into a Run records the Memory ID, version, retrieval reason, rank, namespace,
and policy decision. Raw embeddings and hidden reasoning are not evidence.

### Forgetting is a feature

Memories may expire, be revoked, lose confidence, or be excluded after a policy change. Retention
and deletion are required product behavior.

## Namespace model

Recommended hierarchy:

```text
company/{company_id}
department/{department_id}
project/{project_id}
position/{position_id}
employee/{agent_definition_id}
relationship/{object_type}/{object_id}
user/{principal_id}
```

A Position Memory Policy defines readable and writable namespace patterns. An Implementer may read
company policy, engineering department procedure, project conventions, and its employee feedback
while being denied Finance relationship memory.

Task-scoped state is not placed in a long-term namespace unless an explicit learning process
promotes it.

## Memory record

```text
MemoryRecord
- id
- company_id
- namespace_type
- namespace_id
- memory_type
- content
- content_digest
- provenance_type
- provenance_id
- proposed_by_run_id
- reviewed_by
- confidence_basis_points
- sensitivity
- status
- supersedes_id
- valid_from
- expires_at
- created_at
- accepted_at
- revoked_at
```

Recommended `memory_type` values:

- `FACT`: externally verifiable stable information.
- `PREFERENCE`: an authorized user or customer preference.
- `DECISION`: an approved decision and its scope.
- `PATTERN`: reviewed recurring observation.
- `PROCEDURE`: a bounded operating method.
- `FEEDBACK`: attributable assessment of an employee output.
- `RELATIONSHIP`: durable context about a customer, supplier, or partner.

Recommended status lifecycle:

```text
CANDIDATE
  → ACCEPTED
  → SUPERSEDED | EXPIRED | REVOKED
  ↘ REJECTED
```

## Provenance

Every candidate identifies a durable source:

- Task/Run/Attempt;
- Artifact Version;
- approved user statement;
- Business Object revision;
- external Resource snapshot;
- imported policy or procedure.

The Service rejects “the model remembers” as provenance. Candidate content derived from several
sources records each source in a bounded `MemoryEvidence` relation.

## Write path

```text
Task completes or reviewer accepts output
  → Memory extractor proposes bounded candidates
  → exact and semantic duplicate search
  → sensitivity and conflict classification
  → Memory Policy decision
  → automatic acceptance for allowed low-risk classes
     or human/role review for material memory
  → immutable accepted record
```

Automatic extraction is optional and off by default. A deterministic extractor fixture is required
for CI. Model-backed extraction must use a separate bounded budget and cannot mark its own output as
verified.

Examples requiring review:

- company strategy;
- customer contractual preference;
- pricing rule;
- security procedure;
- financial threshold;
- negative employee feedback;
- a fact that conflicts with accepted memory.

## Read path

The Context Assembler creates a `MemoryQuery` from:

- Company, Department, Position, and Agent Version;
- Task Goal Contract and current Subtask;
- involved Business Object IDs;
- Tool and data permissions;
- requested memory types;
- maximum count and token budget.

Retrieval pipeline:

```text
Permission filter
  → validity/status filter
  → exact metadata filter
  → optional semantic ranking
  → diversity and conflict handling
  → token-budget truncation
  → retrieval evidence
  → Runtime context
```

The Agent sees source and scope labels. Conflicting accepted memories are both returned with a
conflict marker unless an authoritative supersession exists.

## Memory policy

```text
MemoryPolicy
- id
- version
- readable_namespace_patterns
- writable_namespace_patterns
- allowed_memory_types
- auto_accept_memory_types
- forbidden_sensitivity_levels
- maximum_retrieval_count
- maximum_context_tokens
- default_ttl
- review_role
- extraction_enabled
```

The policy is bound to the Position and snapshotted into the Run context. Changing the policy
affects future retrieval and write decisions without rewriting previous Attempt evidence.

## Employee continuity

Employee memory belongs to the Agent Definition, not a transient Agent Version. Agent Version
instructions can change while reviewed experience remains associated with the employee identity.

Employee memory may contain:

- verified task categories completed;
- reviewer feedback;
- recurring failure patterns;
- successful bounded procedures;
- preferred collaboration patterns supported by evidence;
- qualification and capability evidence.

It must not contain:

- raw chain-of-thought;
- secret values;
- unreviewed personality judgments;
- protected personal data without an explicit lawful business purpose;
- invented emotions or relationships;
- performance claims derived only from model self-assessment.

Authority changes require a new Agent Version, Appointment, capability certification, Tool grant,
or Policy decision. Memory alone cannot promote an employee.

## Relationship memory

Customer and supplier context is stored under the durable Business Object identity:

- communication preference;
- approved brand or delivery requirements;
- prior commitments;
- unresolved issue;
- contract-backed constraint;
- consent and retention metadata.

The relationship record is authoritative for structured fields. Memory stores contextual lessons
and references the record revision; it does not duplicate payment details, passwords, or complete
communications.

## Storage architecture

Initial implementation:

```text
PostgreSQL
├─ memory_records
├─ memory_evidence
├─ memory_reviews
├─ memory_retrievals
├─ memory_policies
└─ optional vector column/index
```

Content may stay in JSONB/text for bounded records. Large source material remains an Artifact or
object-store blob. The Memory record stores a summary and source reference.

Redis may cache authorized search results using a key that includes Policy version, namespace,
query digest, and principal scope. Cache invalidation follows Memory status changes.

## API and application boundary

Recommended operations:

```text
POST   /api/v1/memory/candidates
GET    /api/v1/memory/candidates
POST   /api/v1/memory/candidates/{id}/accept
POST   /api/v1/memory/candidates/{id}/reject
POST   /api/v1/memory/{id}/supersede
POST   /api/v1/memory/{id}/revoke
POST   /api/v1/memory/search
GET    /api/v1/tasks/{task_id}/memory-retrievals
```

Runtime code depends on a `MemoryService` application port, not a LangGraph-specific store API.
A LangGraph Store adapter may implement the port while AgentMesh preserves Policy and audit
semantics.

MCP may expose `memory.search` and `memory.propose` to authorized Agents. It must not expose an
unconditional `memory.write`.

## Security and privacy

- exact tenant/company/namespace authorization precedes vector search;
- embeddings inherit the sensitivity and deletion policy of source content;
- secrets are rejected before persistence;
- audit exports redact content by permission;
- customer and user deletion requests remove or tombstone derived embeddings;
- retrieval is capped to prevent context flooding;
- imported content is treated as untrusted and cannot alter procedure or Policy automatically;
- prompt injection inside remembered content is preserved as data, not executable instruction.

## Delivery slices

### Slice 1 — explicit governed memory

- PostgreSQL schema and application port;
- manual candidate creation from an accepted Artifact or user statement;
- accept, reject, supersede, revoke, expire, and exact search;
- Company/Department/Employee namespaces and Policy enforcement;
- deterministic API and audit tests.

### Slice 2 — context assembly

- role-aware retrieval before a Run;
- Attempt-level retrieval evidence;
- conflict markers and token bounds;
- Office/Console memory inspector;
- retention and deletion maintenance jobs.

### Slice 3 — semantic retrieval

- optional embedding provider profile;
- `pgvector` index and hybrid exact/semantic ranking;
- embedding cost accounting;
- re-embedding on model-version changes;
- evaluation fixture for precision, stale-memory rejection, and namespace isolation.

### Slice 4 — reviewed learning

- candidate extraction after reviewed Tasks;
- low-risk automatic acceptance by Policy;
- department learning review queue;
- employee feedback and procedure candidates;
- offline evaluation before enabling extraction in a Company Template.

## Acceptance criteria

- Two employees may share PostgreSQL while receiving different authorized memory results.
- A Run can reproduce which memory versions entered its context.
- Superseding a memory affects future Runs without rewriting historical evidence.
- Revoked, expired, unauthorized, or conflicting memory is handled deterministically.
- A malicious cross-namespace semantic match cannot bypass exact authorization.
- Memory write, review, retrieval, and deletion operations are auditable.
- The system runs without embeddings and without paid APIs.
- Disabling `organizational_memory` removes retrieval and write paths without affecting Task
  execution.

## Non-goals

- storing every conversation forever;
- simulating human consciousness or emotion;
- allowing Agents to rewrite their instructions or authority through memory;
- using vector similarity as authorization;
- replacing Artifact, repository, CRM, finance, or policy systems of record;
- guaranteeing that model-generated memories are true.
