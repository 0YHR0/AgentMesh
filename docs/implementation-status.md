# Implementation status

Last updated: 2026-09-30 · Status: Alpha, supported single-team v1 baseline

This page describes the current repository, not the full target architecture. The
[v1 scope](v1-completion-scope.md) is implementation-complete and release-qualified for evaluation
and non-critical single-team use. It is **not** a production HA or multi-tenant certification.

## What runs today

| Area | Current capability | Important limit |
| --- | --- | --- |
| Tasks and execution | Direct, independently Reviewed, and Coordinated Subtask DAGs; durable Runs/Attempts, retry, pause/resume, cancellation, Handoffs, Plan Patches, and Artifacts | General active-Run replanning and compensation remain future work |
| Agents and models | Published immutable Agent Versions, role-bound execution, deterministic keyless demo, encrypted OpenAI/DeepSeek model connections in the authenticated Console | A private DeepSeek Direct and three-employee Coordinated run passed; OpenAI has fixture qualification, not a live-provider acceptance claim |
| Collaboration and tools | Governed MCP registry/read tools/idempotent writes; trusted A2A peer registration, delegation, polling, cancellation, and unknown-outcome reconciliation | External providers/peers require configuration; irreversible writes and A2A streaming/push are not supported |
| External notifications | Optional Feishu bot cards for Task completion, failure, pending human review, and governed approval requests, backed by durable PostgreSQL delivery jobs | Gate is off by default; app credentials and a test group are required; inbound commands/approvals are not yet supported |
| Governance | Optional Identity/RBAC, Policy approvals, one-time Permits, budgets, quotas, audit, usage, and Langfuse metadata export | Identity is not enabled by default; do not expose credential entry over anonymous public HTTP |
| Company | Company/Position/Appointment, goals, Operations, Business Objects, reviewed PostgreSQL memory with optional subject-scoped review hints, internal finance controls, and installable Packs | No automatic semantic contradiction verdict; external memory ranking, accounting, payments, and most live connectors are not part of the default product |
| Console | Guided task/team creation, model and employee setup, English/Chinese UI, work cards, pinned coordinated context transfers, and durable Mission Map replay | Transfer records start with new Runs; a dependency line alone is not evidence of an Agent message. The game-style Office is retired |
| Runtime and operations | PostgreSQL business ledger, Redis Streams delivery, LangGraph checkpointing, Compose, CI, backup/restore runbooks; production-mode startup rejects disabled RBAC and bundled database credentials | The startup guard is only a baseline check, not production qualification; framework-neutral cutover, durability/chaos/HA, HTTPS ingress, and restore evidence remain open |

The default Compose stack uses a deterministic executor and needs no provider key. The public demo
is also deterministic. A separate private DeepSeek acceptance stack completed a Direct task and a
Console-created three-employee Coordinated task on 2026-09-28; see the
[walkthrough with screenshots](scenarios/customer-feedback-deepseek.md). That acceptance does not
qualify production provider availability or prove live OpenAI operation.

## Runtime boundary

LangGraph is the supported default Worker path, not the business source of truth. The
framework-neutral Runtime SDK and LangGraph/subprocess conformance exist behind explicit gates.
The A4.2d qualification result is `qualified_with_documented_exceptions`: success and reviewed
fixtures match, while failure, budget, cancellation, and unknown-outcome differences remain
documented safety boundaries. The coordinated managed-runtime cutover stays disabled by default.
See the [machine-readable report](qualification/a4-2-parity.json) and
[rollback runbook](operations/runtime-direct-cutover-rollback.md).

## What remains

- Configurable deterministic acceptance for Coordinated business deliverables, distinct from
  execution completion ([#187](https://github.com/0YHR0/AgentMesh/issues/187)).
- Durable non-LangGraph reattach/restart and production admission ([#136](https://github.com/0YHR0/AgentMesh/issues/136)).
- Shared Governed Action SDK beyond MCP/A2A ([#137](https://github.com/0YHR0/AgentMesh/issues/137)).
- Repeatable chaos and correctness qualification ([#138](https://github.com/0YHR0/AgentMesh/issues/138)).
- Production hardening, HA/PITR, load and security qualification ([#160](https://github.com/0YHR0/AgentMesh/issues/160)).
- Optional external integrations and scenarios; see the [current proposals](proposals/README.md).

For configuration and use, start with the [user guide](getting-started.md) or
[administrator guide](best-practices.md). For detailed contracts, use the
[L2 module index](architecture/modules/README.md) and [ADRs](adr/README.md). Earlier incremental
verification records remain available in Git history and pull requests, not in this current-state page.
