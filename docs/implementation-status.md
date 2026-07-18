# Implementation status

Status: Active
Last updated: 2026-07-18

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

The formal L2 implementation is approximately **65% complete**. This is an evidence-based maturity
estimate rather than a count of files: the runnable local control-plane path is about **82%**, while
federated A2A, identity/policy, the Web Console, and production operations remain substantial work.
Phase 1 is about **92%**, Phase 2 about **90%**, and Phase 3 about **70%** against the roadmap exit
criteria.

## Formal module progress

| Formal L2 module | Runtime status | Implemented evidence | Major remaining scope |
|---|---|---|---|
| Cross-module contracts | Partial | Versioned `MessageEnvelope`, idempotency, correlation, immutable `PrincipalContext`, canonical ActionIntent hash, one-time Permit, structured Handoff, Artifact and Tool audit contracts | Delegation, obligations, A2A correlation and full compatibility fixtures |
| Task and execution domain | Implemented baseline | Task/Subtask/Run/Attempt/Handoff ledger, immutable DAG plan, cancellation, fenced leases, durable direct pause/resume, structured acceptance criteria, bounded reviewed execution, immutable Task budget contracts, and audited `WAITING_APPROVAL` resolution | Dynamic plan replacement, Subtask budget slices and general coordinated pause/resume |
| Persistence and consistency | Implemented baseline | PostgreSQL UoW, Alembic, Outbox/Inbox, idempotency, JSONB, LangGraph checkpoints, bounded list queries and bounded messaging cleanup | Reconciliation, archival, partitioning and broker-loss recovery |
| Orchestrator and scheduler | Partial | Durable direct workflow, independent Executor/Reviewer Runs, bounded local Subtask DAG scheduling, capability/version binding, accepted Handoff routing/context, structured dependency output flow, Supervisor join, checkpoint recovery, Worker reclaim, Attempt lease renewal, and Task-level Run/Attempt/Token/cost/deadline admission | Dynamic replanning, hierarchical quota/fairness admission and remote coordination |
| Local Agent Runtime | Partial | Deterministic version-bound Agent and one gated MCP-backed execution path | Real model providers, sandboxing, context assembly and governed Tool loop |
| Agent Registry | Implemented baseline | Definitions, immutable versions, capabilities, deployments, instances and Agent binding | Health reconciliation, rollout policy and remote peer integration |
| MCP integration | Partial | Allowlisted read-only stdio Tool with schema checks, confinement, limits and durable audit | Private registry, Streamable HTTP, credentials, policy/approval and write Tools |
| A2A integration | Not started | Formal L2 target only | Agent Card import, peer trust, delegation, streaming/push/poll and state convergence |
| Artifact Service | Partial | Gated immutable inline-small text/JSON versions with hashing and verified download | Object storage, upload grants, scanning, access grants and retention |
| Policy and approval | Partial | Versioned deterministic decisions, durable GovernedAction, append-only ApprovalDecision, separation of duties and one-time Permit enforcement for Agent publish and budget increase | Conditional/external engine, obligations, quorum/stages, supersession, transactional outcome reconciliation and governed write Tools |
| Event Relay | Implemented baseline | SKIP LOCKED claims, Redis Streams publication, retry, poison-row quarantine, consumer Inbox deduplication, pending-safe retention and Prometheus capacity metrics | Authorized replay, admission backpressure and broker-loss recovery |
| Observability and evaluation | Partial | Durable Attempt trace IDs, usage/cost ledger, conservative reservation/actual settlement, acceptance result history, basis-point quality scores and optional privacy-safe Langfuse export | Semantic/async evaluation, provider price catalogs, OTel operations, SLOs and alerting |
| Identity, tenancy and secrets | Partial | Opt-in digest Bearer authentication, immutable Principal context, tenant binding, default-deny RBAC and authenticated intervention audit actors | OIDC/workload identity, persistent bindings, delegation, RLS/multi-tenancy, SecretReference and Credential Broker |
| Control API | Implemented baseline | Direct, reviewed, coordinated, Handoff, and human resolution commands plus authenticated/RBAC-gated Registry, Artifact, MCP audit, usage, budget and feature APIs with bounded Task/Artifact list loading | Pagination projections, realtime status, persistent identity administration and operations APIs |
| Web Console | Not started | OpenAPI documentation is the current inspection surface | Task/Agent/run monitoring, intervention, approvals and operations UI |
| Deployment and operations | Partial | Docker Compose topology, health/readiness, migrations, free CI, CodeQL and protected `main` | Production topology, backup/restore, HA, capacity controls and release automation |

Supporting delivery infrastructure is also implemented: feature-gated capability profiles and the
free GitHub CI/PR governance baseline are required for every new module increment.

## Delivery queue

The next work is ordered by dependency and operational risk:

1. Add persistent Principal/RoleBinding administration and an OIDC adapter.
2. Expand MCP into a governed registry/gateway and apply Policy to write-capable Tools.
3. Add federated A2A Agent Card import and trusted peer delegation.
4. Extend admission with tenant/project quota fairness and versioned dynamic replanning.
5. Add the Web Console when authenticated intervention and approval contracts are stable.

The rollout-group proposal in [#26](https://github.com/0YHR0/AgentMesh/issues/26) remains separate:
it compares multiple candidate Runs for one work item, while coordinated execution schedules
distinct dependent Subtasks. It requires an accepted architecture contract before implementation.
