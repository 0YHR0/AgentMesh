# Implementation status

Status: Alpha baseline
Last updated: 2026-08-19

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

## Control Plane P0 status

The framework-neutral Control Plane design is accepted and the Runtime A0 contract slice is merged
into the repository baseline. The current runnable path remains the
LangGraph-centric alpha baseline described below. A0 provides the framework-neutral Runtime SDK,
canonical JSON/digest rules, bounded validators, common Envelope, contract fixtures, a fake adapter
conformance skeleton, and architecture dependency checks. It does not add Runtime persistence,
change Worker behavior, provide a LangGraph or subprocess adapter, or claim support for atomic
Permit-use reservation or the chaos qualification report; those remain later P0 slices.

Implementation must follow the [P0 implementation plan](architecture/control-plane-p0-implementation-plan.md)
and its normative Runtime, Governed Action, and reliability specifications. Progress is tracked by
[Epic #134](https://github.com/0YHR0/AgentMesh/issues/134); design checkmarks in the roadmap must not
be interpreted as implemented product capability.

The supported v1 boundary, external-infrastructure dependencies, and intentionally deferred
capabilities are fixed in [v1-completion-scope.md](v1-completion-scope.md).

Verified A0 evidence (2026-08-17, merged baseline):

- Runtime v1 DTO and strict JCS canonicalization tests pass, including binary64 number
  round-trips, digest vectors, size/depth bounds, closed discriminators, unknown
  major/obligation rejection, and bounded/redacted validation errors.
- The black-box fake adapter proves stable dispatch replay, terminal inspection, and the adapter
  port shape without importing application, persistence, transport, or framework packages.
- Architecture tests scan every public Runtime SDK module for forbidden inward/framework imports.

Verified A1 implementation (2026-08-17, merged baseline):

- The expand migration, framework-neutral runtime domain state machine, tenant-scoped PostgreSQL
  repository, gated operator read routes, and TaskRun runtime binding fields are present in the
  A1 change set.
- The repair revision adds ORM/migration constraint and index parity checks, immutable repository
  projections, authenticated-principal visibility tests, lifecycle/observation evidence handling,
  and a real-PostgreSQL schema/bootstrap test module.
- The PostgreSQL integration job passed the schema, deterministic seed, dispatch/idempotency,
  concurrency, owner-fencing, observation-evidence, reattach, visibility, and unresolved-outcome
  tests. Migration upgrade/downgrade/re-upgrade and `alembic check` passed, as did the repository's
  full CI checks. The operator API remains feature-gated and read-only; A1 does not implement an
  adapter or worker switch.

Verified A2 implementation (2026-08-18, CI-validated):

- Legacy Run execution remains authoritative. A separate `managed_runtime_worker` gate is not in
  any profile; A1 registry/query enablement cannot switch existing or unpinned Runs.
- Queued Runs can snapshot `deterministic_shadow` admission with a Runtime Version and generated
  execution-intent identity. The 0046 expand migration persists the admission fields and an
  append-only, attempt-scoped comparison audit; managed shadow evidence is recorded only after
  the provider call and never changes Task/Run state.
- The framework-neutral coordinator performs Runtime prepare/owner claim in short transactions,
  closes the UoW before adapter validation/dispatch, then records canonical observation evidence
  in a new transaction. A2's LangGraph adapter is limited to deterministic inline execution;
  it requires injected state/lifecycle backends and rejects unsupported async, event, pause,
  cancel, and resume capabilities. Production worker wiring fails closed without durable
  backends; ephemeral backends are test-only.
- Unit/API/adapter tests and Ruff passed for this slice. GitHub CI additionally passed the real
  PostgreSQL migration/seed and 41-test A2 integration suite, Compose E2E, coverage, quality, and
  CodeQL checks.
- This remains a deterministic inline shadow path with legacy execution authoritative. The current
  bootstrap is test-only and uses ephemeral state/lifecycle backends; no production durable
  worker backend or production cutover is claimed.

Verified A3 implementation (2026-08-19, PR #149 CI-validated):

- An independently buildable reference Agent package under `examples/reference-agent` imports only
  the public Runtime SDK and Python standard library. Its wheel and the AgentMesh wheel are built,
  installed without dependencies into an isolated temporary environment, and executed with the
  repository `PYTHONPATH` removed.
- The generic subprocess adapter provides structured argv, execution workspaces, environment
  allowlisting, bounded incremental stdout/stderr, process-group cancellation, timeout/error
  mapping, redacted evidence, atomic Artifact staging, stable dispatch identity, and honest
  process-boundary-only isolation wording. The reference fixture includes child+grandchild cleanup
  evidence and controlled malformed/crash/oversize cases.
- A3 remains an adapter proof only: it is not enabled in Worker profiles, does not change legacy
  Task/Run/Attempt authority, does not claim durable reattach or an OS sandbox, and is not deployed.

A4.0 conformance harness (in progress, branch `agent/control-plane-runtime-a4-conformance`):

- A reusable black-box suite is being added for the public `ManagedAgentRuntime` port. The same
  capability-driven matrix runs LangGraph deterministic inline and the generic subprocess
  reference Agent, covering descriptor/digest stability, dispatch idempotency/conflicts, identity,
  Artifact references, terminal error classification, lifecycle behavior, close semantics, and
  honest `reattach=false` behavior.
- A4.0 does not enable `managed_runtime_worker`, `dual_record_runtime`, or
  `generic_subprocess_runtime`; it does not perform runtime authority cutover. Issues #135/#136
  remain open until the later conformance, chaos, parity, and cutover slices are complete.

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
- kept embeddings and free-form model extraction disabled and out of the baseline.

Automatic Organizational Memory runtime verification on 2026-07-30 additionally:

- passed 400 non-PostgreSQL tests at 82.75% line coverage (gate: 80%), including the Admin/Office
  Memory Inspector and lifecycle-listing API;
- injects only accepted, Policy-authorized, bounded Memory into executor Runs;
- records Task/Run-correlated retrieval evidence and skips automatic reviewer context;
- atomically captures up to five structured Task-result candidates with evidence and capped
  confidence when Policy extraction is enabled;
- ships a candidate-set-preserving ranking interface while retaining PostgreSQL as authority;
- provides an Admin Memory inspector for review, revocation, policies, canonical records, and
  Task/Run retrieval trails, plus per-employee Office recall/learning counts;
- requires no external Memory service or API key; Mem0/MemOS adapters remain opt-in proposals.

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

Company Pack in-place upgrade verification on 2026-08-03 additionally:

- passed 415 non-PostgreSQL tests at 83.06% line coverage (gate: 80%);
- added migration `20260803_0044`, revisioned installations, immutable upgrade audit records,
  digest-pinned preview/execute APIs, idempotent replay, and an upgrade Outbox event;
- implemented a deliberately narrow compatibility kernel that validates every current Business
  Object revision and lifecycle state before upgrading a published Type in place;
- proved Music Studio `0.2.0` to `0.3.0` preserves the installation, Type IDs, existing release
  objects, and historical revisions while exposing a guided upgrade card in the product UI;
- continues to reject resource additions/removals, non-Business-Object resource changes, stale
  previews, incompatible schemas, and downgrade attempts.

Company Pack SDK separation phase 1 on 2026-08-03 additionally:

- passed 418 non-PostgreSQL tests at 83.09% line coverage (gate: 80%);
- introduced a stable `CompanyTemplateDefinition` and deterministic `PackCatalog` discovery API;
- moved the Music Studio manifest and configuration validation into
  `agentmesh.packs.music_studio`, retaining a compatibility import for existing consumers;
- changed generic preview, installation, upgrade-preview, and upgrade operations to accept a Pack
  definition instead of importing a concrete business scenario;
- kept workflow-service, route, provider, and UI distribution extraction as a later phase after
  the SDK compatibility and external trust/loading contracts stabilize.

Company Pack SDK separation phase 2 on 2026-08-03 additionally:

- passed 419 non-PostgreSQL tests at 83.11% line coverage (gate: 80%);
- moved the Music Studio runtime service, deterministic provider adapters, HTTP routes, and all
  focused-workspace assets under `agentmesh.packs.music_studio`;
- reduced the generic API and bootstrap modules to explicit scenario composition roots while
  preserving every existing HTTP URL;
- retained compatibility-only modules for all pre-alpha import paths and added identity tests so
  downstream users receive the same implementation during the transition;
- left external executable Pack loading deliberately unsupported until a signed trust and runtime
  extension protocol is specified.

Runtime Extension API v0.1 on 2026-08-03 additionally:

- passed 426 non-PostgreSQL tests at 83.25% line coverage (gate: 80%);
- introduced a trusted in-process extension manifest, registry, entry-point discovery, controlled
  core-service context, exact service-surface validation, health probe, and stop lifecycle;
- added explicit `AGENTMESH_RUNTIME_EXTENSIONS` allowlisting, fail-fast unknown identifiers, and
  fail-closed disabled service/workspace access;
- exposed `GET /api/v1/extensions` for version, health, Feature, Credential, permission, workspace,
  service, and external-write disclosure;
- migrated Music Studio to the generic runtime so the core API/bootstrap no longer imports its
  routes, console, runtime service, or providers;
- kept installed Python extensions explicitly trusted and deferred signatures, sandboxing, hot
  reload, extension migrations, and remote execution to later protocol revisions.

External Runtime Extension proof on 2026-08-03 additionally:

- published the independent `AgentMesh-Extension-Starter` repository with the
  `community.daily-brief` scenario;
- proved wheel metadata discovery through `agentmesh.runtime_extensions` without modifying the
  AgentMesh source tree;
- verified Music Studio and Daily Brief can coexist in one registry without route, workspace, or
  asset collisions;
- added free GitHub CI covering lint, extension tests, integration against AgentMesh, and wheel
  construction.

Trusted Extension Installation baseline on 2026-08-03 additionally:

- added the strict, versioned `extensions.lock` operator allowlist for built-in and external
  extensions;
- rejected installed but unlisted Entry Points before Python import, then checked the loaded
  manifest against locked capability and risk declarations;
- added wheel metadata and SHA-256 preflight, no-dependency installation, and JSONL installation
  receipts through `agentmesh-extension-install`;
- exposed trust level, source, locked distribution, Entry Point, digest, and lock verification in
  `GET /api/v1/extensions`;
- explicitly retained same-process execution and deferred signatures/attestations and isolation.

Market Intelligence Studio baseline verification on 2026-07-30 additionally:

- passed 390 non-PostgreSQL tests at 82.71% line coverage (gate: 80%);
- added a digest-pinned built-in template with eight departments, 17 Positions, and seven
  published Business Object Types;
- added preflight disclosure of Features, permissions, credentials, resource mutations, active
  Company conflicts, and the default-disabled external-write boundary;
- made Company, resource, persisted configuration, installation evidence, and Outbox creation one
  database transaction;
- added an English/Chinese Admin Console installer and a deterministic offline evidence chain from
  Research Question through independently approved Claim Register and Research Report;
- kept Agent Appointments, recurring Operations, real source collection, publication, outreach,
  pricing commitments, invoicing, and spending out of this safe installation baseline.

Music Studio P0-A verification on 2026-07-31 additionally:

- passed 406 non-PostgreSQL tests at 82.84% line coverage (gate: 80%);
- added the first user-facing scenario as a deterministic, digest-pinned Company Pack;
- defined five focused departments, seven responsibility-bound Positions, and seven versioned
  Business Object Types from Music Project through Final Release Package;
- added list, preview, and one-transaction installation APIs with no credentials or external writes;
- records the deterministic Demo provider, language, genre, use plan, and disabled external-write
  boundary in installation configuration;
- keeps music execution, audio generation, project workspace, and live provider integration in the
  explicit P0-B, P0-C, and P1 delivery slices.

Music Studio P0-B audio foundation on 2026-07-31 additionally:

- accepts bounded `audio/wav` Artifacts only after validating their RIFF/WAVE container and returns
  correct playback/download media metadata;
- adds a credential-free deterministic provider that creates reproducible two-second PCM WAV
  candidates through a stable operation key and creative seed;
- adds deterministic analysis of the actual candidate bytes for duration, format, peak, RMS, and
  clipping evidence;
- passed 411 non-PostgreSQL tests at 83.00% line coverage (gate: 80%);
- adds one product API that appoints six deterministic starter employees and launches a six-stage
  coordinated creative brief, trend, lyrics, production, generation, and listening DAG;
- materializes original Demo lyrics, playable audio, measured audio evidence, a shortlist review,
  a rights manifest, and a version-linked Final Release Package;
- requires a separate owner action to move the release from `IN_REVIEW` to `APPROVED` and preserves
  the same audio Artifact Version across reloads;
- keeps live generation providers and release-package download in the remaining P0/P1 work.

Music Studio bounded-review and focused-workspace verification on 2026-08-02 additionally:

- passed 412 non-PostgreSQL tests at 83.04% line coverage (gate: 80%);
- added a clean default-English Music Studio workspace linked from both the Office and Admin
  Console, with guided installation, brief creation, six-role progress, authenticated WAV
  playback, concise listening evidence, and English/Chinese switching;
- added owner revision requests that name the failed criterion and requested change, create a new
  immutable lyrics/audio/evidence/candidate/review chain, and advance the pending release only
  within its declared maximum round count;
- made revision requests idempotent, rejected in-place changes after approval or after the round
  bound, and retained a separate explicit approval action;
- browser-verified the focused desktop layout and language switch; full Compose/browser workflow
  qualification, candidate comparison, and release-package download remain in P0-C/P0-D.

Music Studio P0-C completion on 2026-08-02 additionally:

- generates two independently versioned and reviewed WAV candidates in every bounded round;
- lets the owner compare both playable candidates and explicitly select one through a governed,
  evidence-backed release lifecycle action;
- shows the creative brief, current specialist phase, employee handoffs, round bound, and blocked
  work directly in the focused English/Chinese workspace;
- creates a deterministic immutable ZIP on approval containing the selected WAV, lyrics, rights
  manifest, and a SHA-256-linked release manifest, then exposes one authenticated download action;
- validates ZIP structure, entry paths, expansion bounds, and integrity before persistence;
- leaves one-command startup, full browser/restart/Compose qualification, and credentialed live
  provider adapters in P0-D/P1.

Market Intelligence Operations Pack verification on 2026-07-30 additionally:

- passed 393 non-PostgreSQL tests at 82.78% line coverage (gate: 80%);
- added a separately previewable and explicitly enabled Pack on top of the minimal Studio;
- extended bounded Pack resources with Operating Cycle, Objective/KR, Initiative, Budget
  Allocation, Memory Policy, and Company Operation support;
- atomically creates one active cycle, one active objective, four measurable KRs, one active
  Initiative, a configured budget boundary, a conservative long-term Memory Policy, and three
  recurring Operations;
- keeps all recurring Operations in `DRAFT`, external writes disabled, and Agent execution
  unstarted until a later explicit activation step;
- added English/Chinese Admin Console preflight, configuration, activation state, and unit/API
  regression coverage.

## Formal module progress

| Formal L2 module | Runtime status | Implemented evidence | Major remaining scope |
|---|---|---|---|
| Cross-module contracts | Implemented baseline | Versioned `MessageEnvelope`, idempotency, correlation, immutable `PrincipalContext`, canonical ActionIntent hash, one-time Permit, immutable Goal Contract, evidence-backed Plan Patch, structured Handoff, Artifact and Tool audit contracts, durable A2A remote correlation, obligations, and frozen v1 compatibility fixtures | New major contract versions require new fixtures and a compatibility window |
| Task and execution domain | Implemented baseline | Task/Subtask/Run/Attempt/Handoff ledger, immutable Goal Contract, versioned Plan Patch before execution and at quiescent budget barriers, completed-node identity/output preservation, immutable DAG plan, cancellation, fenced leases, durable direct pause/resume, structured acceptance criteria, bounded reviewed execution, immutable Task budget contracts, and audited `WAITING_APPROVAL` resolution | Active-Run cancellation/compensation and supersession, Subtask budget slices and general coordinated pause/resume |
| Persistence and consistency | Implemented baseline | PostgreSQL UoW, Alembic, Outbox/Inbox, idempotency, JSONB, LangGraph checkpoints, bounded list queries and bounded messaging cleanup | Reconciliation, archival, partitioning and broker-loss recovery |
| Orchestrator and scheduler | Partial | Durable direct workflow, independent Executor/Reviewer Runs, bounded local Subtask DAG scheduling, capability/version binding, verified pre-execution and quiescent remaining-plan replacement, history/side-effect/budget guards, accepted Handoff routing/context, structured dependency output flow, Supervisor join, checkpoint recovery, Worker reclaim, Attempt lease renewal, Task-level Run/Attempt/Token/cost/deadline admission, and atomic versioned tenant/project concurrent-Attempt quota reservations | Active-Run replanning, cross-tenant weighted fair dispatch, deeper quota scopes and remote coordination |
| Local Agent Runtime | Implemented baseline | Digest-verified Agent Version instruction and runtime-policy binding, zero-credential deterministic execution, per-Agent OpenAI model/limit/SecretReference selection, provider Token/cost accounting, bounded and digest-evidenced context compaction, bounded `store=false` function-call continuation, and audited Agent-allowlisted governed MCP Tool calls | Additional providers, provider streaming and hardened external sandbox adapters |
| Agent Registry | Implemented baseline | Definitions, immutable versions, capabilities, deployments, instances, Agent binding and stale-heartbeat health reconciliation | Advanced rollout policy and remote peer adapters |
| Virtual Company OS | Partial | Explicitly gated Company, generic Organization Unit, Position, capability-qualified immutable Agent Version Appointment, organization relationship graph, Operating Cycle/Objectives/Key Results/Initiatives, verified-versus-estimated measurements, Initiative-launched Task lineage, recurring/manual Operations with SKIP LOCKED trigger claims, deterministic occurrence/Task idempotency, bounded missed-run/retry policies, versioned JSON Schema Business Object Types, named lifecycle actions, optimistic concurrency, append-only revisions, evidence and sensitive-field redaction, versioned namespace Memory Policies, candidate/review/supersession/revocation/expiry lifecycle, automatic governed Run context and structured post-Task candidate capture, exact bounded retrieval with conflict and Task/Run audit evidence, hierarchical financial allocations, append-only reserve/release/settlement ledger, classified economic evidence, expense review separation, declarative digest-pinned Company Packs with safe in-place Business Object Type upgrades, one-transaction Market Intelligence Studio installation with offline evidence-chain example, separately activated Operations Pack, template-driven capability-matched workforce Appointments, atomic staffed-Operation preflight/activation, Position-attributable coordinated Task generation, and a provider-neutral live market-research workflow with read-only MCP/tool/version preflight, five coordinated stages, a bounded structured evidence bundle, successful Tool Invocation verification, idempotent draft Source Record and Claim Register creation, internal report Artifact lineage, draft Research Report creation, automatic completion projection, and operator status/retry APIs, PostgreSQL/Alembic persistence, tenant-scoped API, RBAC, domain events, and Office employee/goal projection | Memory retention/semantic and external ranking adapters, Task-cost allocation linkage, accounting adapters, broader Pack resource migrations/downgrade, coordinated Initiative launch, richer calendar/event triggers, Company Metrics, broader Admin Console authoring, and a durable projection-retry queue for failures that outlive the execution worker |
| MCP integration | Implemented baseline | Durable Server/Version/Tool Registry, immutable Schema/configuration digests, side-effect classification, Policy-gated write admission, default-deny Catalog resolution, confined stdio, governed Streamable HTTP reads, Permit-bound idempotent writes, stable operation keys, bounded same-key retry, explicit unknown outcomes, evidence-backed operator convergence, Credential Broker Bearer injection, bounded capability refresh and per-Version circuit breaking | Irreversible writes remain fail-closed; OAuth, Resources/Prompts and background discovery require external adapters |
| A2A integration | Partial | Tenant-scoped trusted Peers, immutable A2A v1 Agent Card snapshots, pinned-HTTPS well-known discovery with ETag/TTL, candidate-only discovery and explicit activation, endpoint allowlists, declared Skill candidates, expiry-aware resolution, Permit-bound HTTP+JSON delegation, workload-bound HTTP Bearer credentials, durable RemoteTaskCorrelation, send-once outcome-unknown handling, evidence-backed remote ID binding/non-delivery convergence, explicit polling, SKIP LOCKED automatic reconciliation, crash-recoverable poll/cancel leases, bounded failure backoff, idempotent best-effort remote cancellation and local state convergence | Streaming/push, richer authentication schemes and Artifact transfer |
| Artifact Service | Implemented baseline | Immutable text/JSON versions, inline-small or content-addressed local blob storage, clean scan state, SHA-256 verification on download and Run lineage | Cloud object-store, malware/DLP, upload-grant and retention adapters |
| Policy and approval | Implemented baseline | Versioned deterministic decisions, structured obligations, durable GovernedAction, append-only per-stage ApprovalDecision, role-constrained ordered stages, quorum, separation of duties and one-time Permit enforcement | External policy engines and action supersession adapters |
| Event Relay | Implemented baseline | SKIP LOCKED claims, Redis Streams publication, retry, poison-row quarantine, consumer Inbox deduplication, pending-safe retention and Prometheus capacity metrics | Authorized replay, admission backpressure and broker-loss recovery |
| Observability and evaluation | Implemented baseline | Durable Attempt trace IDs, usage/cost ledger, operator-versioned price catalogs, conservative reservation/actual settlement, acceptance history, basis-point quality scores, privacy-safe Langfuse export and documented v1 SLOs | Semantic/async evaluator and OTel backend adapters |
| Identity, tenancy and secrets | Partial | Opt-in digest bootstrap and OIDC Bearer authentication, durable user/service Principals, ExternalIdentity/RoleBinding lifecycle, immutable Principal context, tenant/project Task binding, default-deny RBAC, metadata-only SecretReferences, exact A2A/MCP workload CredentialBindings and short-lived lease audit | Groups/delegation, RLS/multi-tenancy, cloud secret providers, OAuth exchange, rotation and mTLS |
| Control API | Implemented baseline | Direct, reviewed, coordinated, Goal/Plan Patch inspection/application, federated A2A delegation/reconciliation/cancellation, MCP/A2A outcome commands, Handoff, human resolution, identity, credential, approval, Registry, Artifact, usage, budget, quota and feature APIs; resumable SSE; cursor-paginated activity and redacted interaction projections; shared replay-bookmark CRUD | Tenant-wide search/export remains a post-v1 audit-index extension |
| Web Console | Implemented baseline | Zero-build Admin Console; SVG Mission Map; lightweight Phaser AgentMesh Office; opt-in Babylon.js 2.5D primary Office with direct/coordinated Task creation, eight-space default campus, PostgreSQL-backed employee grid placements and tenant-shared custom-space definitions, server-derived department moves, bounded obstacle-aware A* Handoff routes, approval-review travel, truthful station poses, ambient employee activity, and sanitized MCP/A2A/Policy packet projections; durable Handoff/MCP/A2A/Policy/Plan Patch routes; filters; deterministic replay; PostgreSQL-backed shared bookmarks; sanitized export; zoom/pan/focus/minimap; inspector/Event Deck; work-card fallback; Plan Patch editor; Agent lifecycle; Artifact lineage; realtime SSE/poll fallback; deterministic research-brief showcase; English/Chinese Market Intelligence Studio installer, workforce setup, live-research readiness diagnostics and Task launcher | Semantic clustering is deferred beyond the supported 20-Agent Task limit; the 2.5D renderer remains experimental behind `office_3d`; authoritative custom-space employee placement geometry remains limited to the eight standard grid departments |
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
   `organizational_memory`, and internal `financial_governance` foundations: Memory retention and
   semantic/external ranking adapters, Task-cost allocation linkage, accounting evidence adapters,
   additional Pack resource kinds/upgrades, richer triggers, and Company Metrics. Template-driven
   Agent Appointments and explicit staffed-Operation activation are implemented for the installed
   market-intelligence studio.
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
