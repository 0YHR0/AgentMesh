# Implementation status

Status: Active
Last updated: 2026-07-17

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

AgentMesh currently provides a durable asynchronous single-agent path:

```text
Control API -> PostgreSQL Task/Run/Outbox -> Event Relay -> Redis Streams
            -> Execution Worker -> LangGraph checkpoint -> PostgreSQL result/usage
```

The `minimal` feature profile runs this path without external model credentials. The `standard`
and `full` profiles progressively enable management APIs, inline-small Artifacts, a read-only MCP
Tool, and observability.

## Formal module progress

| Formal L2 module | Runtime status | Implemented evidence | Major remaining scope |
|---|---|---|---|
| Cross-module contracts | Partial | Versioned `MessageEnvelope`, idempotency, correlation, Artifact and Tool audit contracts | Principal, Handoff, Approval, A2A correlation and full compatibility fixtures |
| Task and execution domain | Implemented baseline | Task/Run/Attempt ledger, cancellation, fenced leases, durable pause/resume | Subtasks, Handoffs, acceptance criteria, review/revision and budgets |
| Persistence and consistency | Implemented baseline | PostgreSQL UoW, Alembic, Outbox/Inbox, idempotency, JSONB, LangGraph checkpoints, bounded list queries and bounded messaging cleanup | Reconciliation, archival, partitioning and broker-loss recovery |
| Orchestrator and scheduler | Partial | Durable single-Agent workflow, checkpoint recovery, worker reclaim and Attempt lease renewal | DAG scheduling, capability matching, admission control and multi-Agent coordination |
| Local Agent Runtime | Partial | Deterministic version-bound Agent and one gated MCP-backed execution path | Real model providers, sandboxing, context assembly and governed Tool loop |
| Agent Registry | Implemented baseline | Definitions, immutable versions, capabilities, deployments, instances and Agent binding | Health reconciliation, rollout policy and remote peer integration |
| MCP integration | Partial | Allowlisted read-only stdio Tool with schema checks, confinement, limits and durable audit | Private registry, Streamable HTTP, credentials, policy/approval and write Tools |
| A2A integration | Not started | Formal L2 target only | Agent Card import, peer trust, delegation, streaming/push/poll and state convergence |
| Artifact Service | Partial | Gated immutable inline-small text/JSON versions with hashing and verified download | Object storage, upload grants, scanning, access grants and retention |
| Policy and approval | Not started | Formal L2 target only | Policy decisions, action intents, approval lifecycle and enforcement |
| Event Relay | Implemented baseline | SKIP LOCKED claims, Redis Streams publication, retry, poison-row quarantine, consumer Inbox deduplication, pending-safe retention and Prometheus capacity metrics | Authorized replay, admission backpressure and broker-loss recovery |
| Observability and evaluation | Partial | Durable Attempt trace IDs, usage/cost ledger and optional privacy-safe Langfuse export | Evaluation, OTel operations, SLOs, quality scores and alerting |
| Identity, tenancy and secrets | Not started | Tenant IDs are propagated through current business records | Authentication, principals, RBAC, tenant isolation, quotas and secret references |
| Control API | Implemented baseline | Task lifecycle plus gated Registry, Artifact, MCP audit, usage and feature inspection APIs with bounded Task/Artifact list loading | Identity enforcement, pagination projections, realtime status and operations APIs |
| Web Console | Not started | OpenAPI documentation is the current inspection surface | Task/Agent/run monitoring, intervention, approvals and operations UI |
| Deployment and operations | Partial | Docker Compose topology, health/readiness, migrations, free CI, CodeQL and protected `main` | Production topology, backup/restore, HA, capacity controls and release automation |

Supporting delivery infrastructure is also implemented: feature-gated capability profiles and the
free GitHub CI/PR governance baseline are required for every new module increment.

## Delivery queue

The next work is ordered by dependency and operational risk:

1. Deliver reviewed execution: acceptance criteria, Executor/Reviewer roles, bounded revision and
   budget/timeout escalation.
2. Deliver coordinated local Agents: Subtask DAG, capability matching, Handoff and Supervisor.
3. Expand MCP into a governed registry/gateway and then add federated A2A peers.
4. Add identity/policy foundations before enabling high-risk Tools or multi-tenant operation.
5. Add the Web Console when the intervention and approval contracts are stable.

The rollout-group proposal in [#26](https://github.com/0YHR0/AgentMesh/issues/26) is a candidate
bridge between reviewed execution and coordinated execution. It requires an accepted architecture
contract before runtime implementation.
