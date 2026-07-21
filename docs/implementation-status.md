# Implementation status

Status: Active
Last updated: 2026-07-21

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

The formal L2 implementation is approximately **93% complete**. This is an evidence-based maturity
estimate rather than a count of files: the runnable local control-plane path is about **95%**, while
advanced federated A2A execution, advanced Console operations, and production operations remain substantial work.
Phase 1 is about **94%**, Phase 2 about **93%**, Phase 3 about **88%**, the governed MCP Phase 4 about
**81%**, and federated A2A Phase 5 about **78%** against the roadmap exit criteria.

## Formal module progress

| Formal L2 module | Runtime status | Implemented evidence | Major remaining scope |
|---|---|---|---|
| Cross-module contracts | Partial | Versioned `MessageEnvelope`, idempotency, correlation, immutable `PrincipalContext`, canonical ActionIntent hash, one-time Permit, immutable Goal Contract, evidence-backed Plan Patch, structured Handoff, Artifact and Tool audit contracts, durable A2A remote correlation, and immutable evidence-backed external outcome resolutions | Obligations and full compatibility fixtures |
| Task and execution domain | Implemented baseline | Task/Subtask/Run/Attempt/Handoff ledger, immutable Goal Contract, versioned pre-execution Plan Patch, immutable DAG plan, cancellation, fenced leases, durable direct pause/resume, structured acceptance criteria, bounded reviewed execution, immutable Task budget contracts, and audited `WAITING_APPROVAL` resolution | Running-task plan supersession, Subtask budget slices and general coordinated pause/resume |
| Persistence and consistency | Implemented baseline | PostgreSQL UoW, Alembic, Outbox/Inbox, idempotency, JSONB, LangGraph checkpoints, bounded list queries and bounded messaging cleanup | Reconciliation, archival, partitioning and broker-loss recovery |
| Orchestrator and scheduler | Partial | Durable direct workflow, independent Executor/Reviewer Runs, bounded local Subtask DAG scheduling, capability/version binding, verified pre-execution plan replacement, accepted Handoff routing/context, structured dependency output flow, Supervisor join, checkpoint recovery, Worker reclaim, Attempt lease renewal, Task-level Run/Attempt/Token/cost/deadline admission, and atomic versioned tenant/project concurrent-Attempt quota reservations | Running-task replanning, cross-tenant weighted fair dispatch, deeper quota scopes and remote coordination |
| Local Agent Runtime | Partial | Digest-verified Agent Version instruction binding, zero-credential deterministic execution, optional bounded OpenAI Responses API execution with provider Token accounting, and one gated MCP-backed execution path | Additional providers, per-Agent credentials/model policy, streaming, sandboxing, context assembly and governed model Tool loop |
| Agent Registry | Implemented baseline | Definitions, immutable versions, capabilities, deployments, instances and Agent binding | Health reconciliation, rollout policy and remote peer integration |
| MCP integration | Partial | Durable Server/Version/Tool Registry, immutable Schema/configuration digests, side-effect classification, Policy-gated write capability admission, default-deny Catalog resolution, confined stdio, governed Streamable HTTP reads, Permit-bound idempotent writes, stable operation keys, bounded same-key retry, explicit unknown outcomes, evidence-backed operator convergence without replay, Credential Broker Bearer injection, and bounded immutable capability refresh | Non-idempotent/irreversible writes, automatic status queries, authenticated/background discovery, OAuth, health/circuit controls and Resources/Prompts |
| A2A integration | Partial | Tenant-scoped trusted Peers, immutable A2A v1 Agent Card snapshots, pinned-HTTPS well-known discovery with ETag/TTL, candidate-only discovery and explicit activation, endpoint allowlists, declared Skill candidates, expiry-aware resolution, Permit-bound HTTP+JSON delegation, workload-bound HTTP Bearer credentials, durable RemoteTaskCorrelation, send-once outcome-unknown handling, evidence-backed remote ID binding/non-delivery convergence, explicit polling, SKIP LOCKED automatic reconciliation, crash-recoverable poll/cancel leases, bounded failure backoff, idempotent best-effort remote cancellation and local state convergence | Streaming/push, richer authentication schemes and Artifact transfer |
| Artifact Service | Partial | Gated immutable inline-small text/JSON versions with hashing and verified download | Object storage, upload grants, scanning, access grants and retention |
| Policy and approval | Partial | Versioned deterministic decisions, durable GovernedAction, append-only ApprovalDecision, separation of duties and one-time Permit enforcement for Agent publish, budget increase and exact MCP idempotent write execution | Conditional/external engine, obligations, quorum/stages, supersession and transactional outcome reconciliation |
| Event Relay | Implemented baseline | SKIP LOCKED claims, Redis Streams publication, retry, poison-row quarantine, consumer Inbox deduplication, pending-safe retention and Prometheus capacity metrics | Authorized replay, admission backpressure and broker-loss recovery |
| Observability and evaluation | Partial | Durable Attempt trace IDs, usage/cost ledger, conservative reservation/actual settlement, acceptance result history, basis-point quality scores and optional privacy-safe Langfuse export | Semantic/async evaluation, provider price catalogs, OTel operations, SLOs and alerting |
| Identity, tenancy and secrets | Partial | Opt-in digest bootstrap and OIDC Bearer authentication, durable user/service Principals, ExternalIdentity/RoleBinding lifecycle, immutable Principal context, tenant/project Task binding, default-deny RBAC, metadata-only SecretReferences, exact A2A/MCP workload CredentialBindings and short-lived lease audit | Groups/delegation, RLS/multi-tenancy, cloud secret providers, OAuth exchange, rotation and mTLS |
| Control API | Implemented baseline | Direct, reviewed, coordinated, Goal/Plan Patch inspection and application, federated A2A delegation/reconciliation/cancellation, evidence-backed MCP/A2A outcome commands, Handoff, human resolution, persistent identity, credential metadata and approval commands plus authenticated/RBAC-gated Registry, Artifact, MCP audit, usage, budget, quota-policy and feature APIs with bounded lists | Pagination projections, realtime status and operations APIs |
| Web Console | Partial | Built-in zero-build Console for Task creation/list/detail, editable role-to-Agent binding, coordinated dependency visualization, Run assignment/history, output inspection, polling, optional session Bearer token, and run/pause/resume/cancel controls | Agent catalog/version publishing, SSE, scalable graph layout, approvals, artifacts, audit timeline and advanced operations UI |
| Deployment and operations | Partial | Docker Compose topology, health/readiness, migrations, free CI, CodeQL and protected `main` | Production topology, backup/restore, HA, capacity controls and release automation |

Supporting delivery infrastructure is also implemented: feature-gated capability profiles and the
free GitHub CI/PR governance baseline are required for every new module increment.

## Delivery queue

The next work is ordered by dependency and operational risk:

1. Add a governed model Tool loop and per-Agent provider/model credential binding.
2. Expand the Console with realtime events, Agent management, approval inbox, artifacts, and an
   audit timeline only as their reference workflow requires them.
3. Extend Plan Patches to running Tasks only after explicit supersession, cancellation convergence,
   budget redistribution, and irreversible-side-effect guards exist.
4. Introduce a cross-tenant dispatcher for weighted fair scheduling, deadline aging, and a reserved
   recovery lane; tenant/project hard quota admission is now implemented.

The rollout-group proposal in [#26](https://github.com/0YHR0/AgentMesh/issues/26) remains separate:
it compares multiple candidate Runs for one work item, while coordinated execution schedules
distinct dependent Subtasks. It requires an accepted architecture contract before implementation.
