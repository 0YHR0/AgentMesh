# Implementation status

Status: Alpha baseline
Last updated: 2026-07-28

This page records what the repository actually implements. The formal L2 documents describe the
target architecture; an implemented vertical slice does not imply that every capability in its
formal module is complete.

## Status vocabulary

- **Implemented baseline**: a runnable, tested capability exists and is part of the supported
  repository baseline.
- **Partial**: at least one tested vertical slice exists, but important target contracts remain.
- **Not started**: only target design exists; there is no supported runtime slice yet.

Every implementation pull request must update this page when it changes module maturity or the
next delivery queue.

The supported v1 boundary, external-infrastructure dependencies, and intentionally deferred
capabilities are fixed in [v1-completion-scope.md](v1-completion-scope.md).

## Current runnable baseline

AgentMesh currently provides durable direct, independently reviewed, and coordinated Subtask DAG
execution paths:

```text
Control API -> PostgreSQL Task/Run/Outbox -> Event Relay -> Redis Streams
            -> Execution Worker -> LangGraph checkpoint -> PostgreSQL result/usage
```

The `minimal` feature profile runs the direct path without external model credentials. The
`standard` and `full` profiles progressively enable reviewed execution, coordinated local Agents,
management APIs, audited human resolution, inline-small Artifacts, a read-only MCP Tool,
observability, and Task budgets.

## Delivery progress snapshot

The supported single-team v1 scope is **100% implementation-complete and verified**. The broader
formal L2 architecture intentionally includes post-v1 infrastructure
adapters and multi-tenant/HA targets; those are not included in the v1 percentage. See the
completion boundary above rather than interpreting this statement as full cloud-production
certification.

Verification evidence on 2026-07-23:

- 326 non-PostgreSQL tests passed with 82.49% line coverage (gate: 80%).
- 19 isolated PostgreSQL/Redis integration tests passed.
- Alembic `check`, one-step downgrade, and re-upgrade passed through revision `20260723_0033`.
- Compose E2E passed direct, independently reviewed, and coordinated Plan-Patch Task paths.
- Browser verification confirmed the 20-Agent Mission Map and a shared replay bookmark surviving
  a full page reload.
- The backup command produced a SHA-256-manifested PostgreSQL + Artifact bundle.

Alpha release qualification on 2026-07-27 additionally:

- restored a backup into an isolated Compose project after deliberately removing every database
  table;
- recovered 4 Tasks, 10 Runs, 9 Attempts, one content-addressed Artifact, one shared replay
  bookmark, and one governed action;
- verified the restored 70,023-byte Artifact against its recorded SHA-256 digest;
- reran direct, independently reviewed, and coordinated Compose E2E paths after restore.

Office renderer verification on 2026-07-28 additionally:

- kept the supported lightweight Phaser Office at `/world`;
- added the explicitly gated `office_3d` Babylon.js 2.5D operator projection at `/world-3d`;
- browser-verified real Agent/Task projection, employee focus, department navigation, zoom,
  English/Chinese switching, and the WebGL fallback boundary;
- self-hosted the renderer and license assets so the Console has no third-party runtime CDN
  dependency.

Office Runtime verification on 2026-07-29 additionally:

- added a PostgreSQL-backed 35 x 12 Office grid and eight authoritative department boundaries;
- made employee drops snap to legal, unoccupied cells with server-derived cross-department moves;
- browser-verified that a persisted Agent placement survives a full page navigation and reload;
- added rendering-only employee roaming, work-tablet motion, foliage sway and lamp pulse, with
  Handoff animation retaining priority;
- kept custom room geometry browser-local until an authorized shared room-layout contract exists.

Office semantic-runtime verification on 2026-07-29 additionally:

- added bounded A* employee routing over the authoritative 35 x 12 grid;
- made persisted Handoffs animate to a reachable adjacent cell with an illuminated route;
- made waiting-for-approval Agents visit Operations while blocked and working Agents retain
  truthful station behavior;
- added server-declared furniture cells to placement validation and route avoidance;
- browser-verified the versioned renderer, semantic employee labels, eight-room/16-obstacle layout,
  and WebGL fallback boundary without frontend errors.

Office shared-layout and governed-interaction verification on 2026-07-29 additionally:

- moved bounded custom-space definitions from per-browser storage to a tenant-scoped PostgreSQL
  layout contract with authorized create/reset operations and a one-time compatibility import;
- projected sanitized MCP, A2A, and Policy events into accessible cards and short-lived Babylon.js
  data packets without exposing governed payloads or advancing Task state;
- disabled packet motion under `prefers-reduced-motion` while retaining the accessible event feed.

Virtual Company Operations verification on 2026-07-29 additionally:

- passed 366 non-PostgreSQL tests at 82.52% line coverage (gate: 80%);
- added migration `20260729_0038` and PostgreSQL fixtures for two-scheduler `SKIP LOCKED`
  exclusion plus durable occurrence-to-Task lineage;
- verified deterministic interval/manual occurrence keys, all four missed-schedule policies,
  concurrency/run-window admission, bounded retries, and operator-visible exception evidence;
- kept `company_operations` explicit opt-in and outside the existing `full` profile.

Typed Business Objects verification on 2026-07-29 additionally:

- passed 372 non-PostgreSQL tests at 82.51% line coverage (gate: 80%);
- added migration `20260729_0039` and PostgreSQL revision/optimistic-concurrency fixtures;
- verified version publication/deprecation, JSON Schema enforcement, named action state/field
  admission, Position/capability/evidence requirements, stale-write rejection, append-only
  history, and sensitive-field redaction;
- kept `business_objects` independently opt-in on `company_model` without requiring the scheduler.

Organizational Memory verification on 2026-07-29 additionally:

- passed 379 non-PostgreSQL tests at 82.56% line coverage (gate: 80%);
- added migration `20260729_0040` and PostgreSQL supersession/retrieval-evidence fixtures;
- verified exact namespace-before-content authorization, versioned Policy invalidation,
  candidate/review/auto-accept lifecycle, durable provenance, secret rejection, supersession,
  revocation, expiry, conflict marking, bounded retrieval, and Task/Run-ready audit records;
- kept embeddings, model extraction, and automatic Run injection disabled and out of the baseline.

Financial Governance verification on 2026-07-29 additionally:

- passed 383 non-PostgreSQL tests at 82.58% line coverage (gate: 80%);
- added migration `20260729_0041` and PostgreSQL hierarchy/ledger round-trip fixtures;
- verified ancestor-mirrored atomic budget reservations, idempotent operation keys, bounded
  release/settlement, immutable classified economic evidence, expense separation of duties, and
  an estimated-versus-verified owner dashboard;
- kept accounting connectors, commercial writes, payment requests, and real-money adapters
  disabled and outside the baseline.

Company Packs verification on 2026-07-29 additionally:

- passed 386 non-PostgreSQL tests at 82.61% line coverage (gate: 80%);
- added migration `20260729_0042` and PostgreSQL atomic resource/installation fixtures;
- verified semantic-versioned immutable manifests, publish validation, dependency and Feature
  preview, digest-pinned idempotent installation, conflict rejection, and transactional creation
  of Organization Units, Positions, and published Business Object Types;
- rejected arbitrary executable Pack code and deferred explicit upgrade/downgrade and remote
  registry trust to later increments.

## Formal module progress

| Formal L2 module | Runtime status | Implemented evidence | Major remaining scope |
|---|---|---|---|
| Cross-module contracts | Implemented baseline | Versioned `MessageEnvelope`, idempotency, correlation, immutable `PrincipalContext`, canonical ActionIntent hash, one-time Permit, immutable Goal Contract, evidence-backed Plan Patch, structured Handoff, Artifact and Tool audit contracts, durable A2A remote correlation, obligations, and frozen v1 compatibility fixtures | New major contract versions require new fixtures and a compatibility window |
| Task and execution domain | Implemented baseline | Task/Subtask/Run/Attempt/Handoff ledger, immutable Goal Contract, versioned Plan Patch before execution and at quiescent budget barriers, completed-node identity/output preservation, immutable DAG plan, cancellation, fenced leases, durable direct pause/resume, structured acceptance criteria, bounded reviewed execution, immutable Task budget contracts, and audited `WAITING_APPROVAL` resolution | Active-Run cancellation/compensation and supersession, Subtask budget slices and general coordinated pause/resume |
| Persistence and consistency | Implemented baseline | PostgreSQL UoW, Alembic, Outbox/Inbox, idempotency, JSONB, LangGraph checkpoints, bounded list queries and bounded messaging cleanup | Reconciliation, archival, partitioning and broker-loss recovery |
| Orchestrator and scheduler | Partial | Durable direct workflow, independent Executor/Reviewer Runs, bounded local Subtask DAG scheduling, capability/version binding, verified pre-execution and quiescent remaining-plan replacement, history/side-effect/budget guards, accepted Handoff routing/context, structured dependency output flow, Supervisor join, checkpoint recovery, Worker reclaim, Attempt lease renewal, Task-level Run/Attempt/Token/cost/deadline admission, and atomic versioned tenant/project concurrent-Attempt quota reservations | Active-Run replanning, cross-tenant weighted fair dispatch, deeper quota scopes and remote coordination |
| Local Agent Runtime | Implemented baseline | Digest-verified Agent Version instruction and runtime-policy binding, zero-credential deterministic execution, per-Agent OpenAI model/limit/SecretReference selection, provider Token/cost accounting, bounded and digest-evidenced context compaction, bounded `store=false` function-call continuation, and audited Agent-allowlisted governed MCP Tool calls | Additional providers, provider streaming and hardened external sandbox adapters |
| Agent Registry | Implemented baseline | Definitions, immutable versions, capabilities, deployments, instances, Agent binding and stale-heartbeat health reconciliation | Advanced rollout policy and remote peer adapters |
| Virtual Company OS | Partial | Explicitly gated Company, generic Organization Unit, Position, capability-qualified immutable Agent Version Appointment, organization relationship graph, Operating Cycle/Objectives/Key Results/Initiatives, verified-versus-estimated measurements, Initiative-launched Task lineage, recurring/manual Operations with SKIP LOCKED trigger claims, deterministic occurrence/Task idempotency, bounded missed-run/retry policies, versioned JSON Schema Business Object Types, named lifecycle actions, optimistic concurrency, append-only revisions, evidence and sensitive-field redaction, versioned namespace Memory Policies, candidate/review/supersession/revocation/expiry lifecycle, exact bounded retrieval with conflict and Task/Run audit evidence, hierarchical financial allocations, append-only reserve/release/settlement ledger, classified economic evidence, expense review separation, declarative digest-pinned Company Packs for organization and object resources, PostgreSQL/Alembic persistence, tenant-scoped API, RBAC, domain events, and Office employee/goal projection | Automatic Memory context injection/retention/semantic search, Task-cost allocation linkage, accounting adapters, Pack upgrades and additional resource kinds, coordinated Initiative launch, richer calendar/event triggers, Company Metrics, the market-intelligence template, and full Admin Console authoring |
| MCP integration | Implemented baseline | Durable Server/Version/Tool Registry, immutable Schema/configuration digests, side-effect classification, Policy-gated write admission, default-deny Catalog resolution, confined stdio, governed Streamable HTTP reads, Permit-bound idempotent writes, stable operation keys, bounded same-key retry, explicit unknown outcomes, evidence-backed operator convergence, Credential Broker Bearer injection, bounded capability refresh and per-Version circuit breaking | Irreversible writes remain fail-closed; OAuth, Resources/Prompts and background discovery require external adapters |
| A2A integration | Partial | Tenant-scoped trusted Peers, immutable A2A v1 Agent Card snapshots, pinned-HTTPS well-known discovery with ETag/TTL, candidate-only discovery and explicit activation, endpoint allowlists, declared Skill candidates, expiry-aware resolution, Permit-bound HTTP+JSON delegation, workload-bound HTTP Bearer credentials, durable RemoteTaskCorrelation, send-once outcome-unknown handling, evidence-backed remote ID binding/non-delivery convergence, explicit polling, SKIP LOCKED automatic reconciliation, crash-recoverable poll/cancel leases, bounded failure backoff, idempotent best-effort remote cancellation and local state convergence | Streaming/push, richer authentication schemes and Artifact transfer |
| Artifact Service | Implemented baseline | Immutable text/JSON versions, inline-small or content-addressed local blob storage, clean scan state, SHA-256 verification on download and Run lineage | Cloud object-store, malware/DLP, upload-grant and retention adapters |
| Policy and approval | Implemented baseline | Versioned deterministic decisions, structured obligations, durable GovernedAction, append-only per-stage ApprovalDecision, role-constrained ordered stages, quorum, separation of duties and one-time Permit enforcement | External policy engines and action supersession adapters |
| Event Relay | Implemented baseline | SKIP LOCKED claims, Redis Streams publication, retry, poison-row quarantine, consumer Inbox deduplication, pending-safe retention and Prometheus capacity metrics | Authorized replay, admission backpressure and broker-loss recovery |
| Observability and evaluation | Implemented baseline | Durable Attempt trace IDs, usage/cost ledger, operator-versioned price catalogs, conservative reservation/actual settlement, acceptance history, basis-point quality scores, privacy-safe Langfuse export and documented v1 SLOs | Semantic/async evaluator and OTel backend adapters |
| Identity, tenancy and secrets | Partial | Opt-in digest bootstrap and OIDC Bearer authentication, durable user/service Principals, ExternalIdentity/RoleBinding lifecycle, immutable Principal context, tenant/project Task binding, default-deny RBAC, metadata-only SecretReferences, exact A2A/MCP workload CredentialBindings and short-lived lease audit | Groups/delegation, RLS/multi-tenancy, cloud secret providers, OAuth exchange, rotation and mTLS |
| Control API | Implemented baseline | Direct, reviewed, coordinated, Goal/Plan Patch inspection/application, federated A2A delegation/reconciliation/cancellation, MCP/A2A outcome commands, Handoff, human resolution, identity, credential, approval, Registry, Artifact, usage, budget, quota and feature APIs; resumable SSE; cursor-paginated activity and redacted interaction projections; shared replay-bookmark CRUD | Tenant-wide search/export remains a post-v1 audit-index extension |
| Web Console | Implemented baseline | Zero-build Admin Console; SVG Mission Map; lightweight Phaser AgentMesh Office; opt-in Babylon.js 2.5D primary Office with direct/coordinated Task creation, eight-space default campus, PostgreSQL-backed employee grid placements and tenant-shared custom-space definitions, server-derived department moves, bounded obstacle-aware A* Handoff routes, approval-review travel, truthful station poses, ambient employee activity, and sanitized MCP/A2A/Policy packet projections; durable Handoff/MCP/A2A/Policy/Plan Patch routes; filters; deterministic replay; PostgreSQL-backed shared bookmarks; sanitized export; zoom/pan/focus/minimap; inspector/Event Deck; work-card fallback; Plan Patch editor; Agent lifecycle; Artifact lineage; realtime SSE/poll fallback; deterministic research-brief showcase | Semantic clustering is deferred beyond the supported 20-Agent Task limit; the 2.5D renderer remains experimental behind `office_3d`; authoritative custom-space employee placement geometry remains limited to the eight standard grid departments |
| Deployment and operations | Implemented baseline | Docker Compose, readiness, migrations, free CI/CodeQL, protected `main`, coverage gate, verifiable PostgreSQL+Artifact backup/restore drill, SLO/RPO/RTO runbook and tag-driven GitHub release assets | Managed HA, PITR and cluster capacity certification require target infrastructure |

Supporting delivery infrastructure is also implemented: feature-gated capability profiles and the
free GitHub CI/PR governance baseline are required for every new module increment.

## Post-v1 delivery queue

There are no open locally verifiable items in the accepted v1 completion scope. The next work
requires an explicit proposal or target infrastructure:

1. Validate the product with the proposed
   [governed software delivery team](proposals/governed-software-delivery-team.md): deterministic
   vertical slice, configuration preflight, model-backed local workflow, and governed draft-PR
   publication.
2. Continue the accepted
   [Virtual Company operating model](proposals/virtual-company-operating-model.md) after the
   implemented `company_model`, `company_goals`, `company_operations`, `business_objects`,
   `organizational_memory`, and internal `financial_governance` foundations: automatic Memory
   context injection and retention, Task-cost allocation linkage, accounting evidence adapters,
   additional Pack resource kinds/upgrades, and the deterministic
   market-intelligence studio template.
3. Active-Run supersession and compensation before widening Plan Patches beyond quiescent barriers.
4. Cloud object storage/scanning, OAuth/cloud secret exchange, A2A streaming/push and OTel adapters.
5. Managed PostgreSQL PITR/HA and Kubernetes capacity certification.
6. Tenant-wide audit search/export if bounded Task projections prove insufficient.

Cross-tenant weighted fair dispatch is intentionally deferred for the current single-team release
and is recorded as a [proposal](proposals/cross-tenant-fair-dispatch.md). Implement it only when a
shared Worker pool has real multi-tenant contention evidence.

The rollout-group proposal in [#26](https://github.com/0YHR0/AgentMesh/issues/26) remains separate:
it compares multiple candidate Runs for one work item, while coordinated execution schedules
distinct dependent Subtasks. It requires an accepted architecture contract before implementation.
