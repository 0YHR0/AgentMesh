# L0 — AgentMesh system boundary

Status: Current architecture summary · Updated: 2026-09-29

AgentMesh is a self-hostable control plane for one or more specialized AI agents working on a
bounded user goal. It owns the durable task lifecycle, identity of the responsible employee,
handoffs, approvals, budgets, evidence, and recovery. A model's conversation or a workflow
engine's checkpoint is not the business record.

## Product outcome

The owner defines a goal, source material, acceptance criteria, and optional limits. AgentMesh
supports one employee for simple work, an independent review when quality warrants it, or a
dependency-driven specialist team. The owner can inspect status and outputs, intervene at defined
boundaries, and accept the resulting Artifacts. The Console is the primary user interface;
the game-style Office has been retired.

## Actors and trust boundaries

| Actor | Responsibility |
| --- | --- |
| Task owner | States the goal and success conditions; reviews the result |
| Operator / approver | Handles incidents and decides governed actions |
| Agent author | Publishes immutable role, capability, instruction, tool, and model versions |
| Local or remote agent | Executes an assigned Run; its output is evidence, not authority to change business state |
| Tool or external service | Supplies context or performs a policy-controlled action; is not trusted by default |

The control plane authenticates and authorizes commands when Identity is enabled. MCP tools and
A2A peers have separate registration and trust boundaries. High-risk actions require an exact
approval/Permit path. Credentials must not appear in task content, prompts, or public HTTP pages.

## Sources of truth

- PostgreSQL owns Task, Run, Attempt, Agent Version, Policy, Permit, Handoff, Artifact metadata,
  usage, audit, and other business records.
- Redis Streams carries work; duplicate or delayed delivery cannot create new business truth.
- LangGraph checkpoints support recovery of the default local executor. They do not decide Task
  completion. Framework-neutral Runtime contracts remain opt-in and qualification-gated.
- Langfuse and metrics are observability sinks, not billing or workflow authority.

## Current boundary

The supported Alpha baseline is single-team evaluation and non-critical deployment. Direct,
Reviewed, and Coordinated paths run; advanced tools, federation, governance, company records, and
memory are opt-in. Production HA/PITR, cross-tenant isolation/fairness, and default managed-runtime
cutover are outside the support claim. See [implementation status](../implementation-status.md),
[v1 scope](../v1-completion-scope.md), and [ADR 0007](../adr/0007-framework-neutral-agent-control-plane.md).
