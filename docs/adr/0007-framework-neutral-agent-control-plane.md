# ADR 0007: Make AgentMesh a framework-neutral Agent Control Plane

Status: Accepted
Date: 2026-08-16
Decision owners: AgentMesh maintainers

## Context

AgentMesh already owns durable Task/Run/Attempt state, scheduling, lease/fencing, Agent Registry,
Policy/Approval/Permit, MCP/A2A governance, Artifact lineage, budget admission, audit, and recovery.
The current execution composition nevertheless routes most local work through a LangGraph-oriented
`WorkflowRunner` and a synchronous `AgentExecutor`. This makes the product appear to be another
multi-Agent framework and leaves framework neutrality as a documentation claim.

LangGraph, OpenAI Agents SDK, CrewAI, Codex, Claude Code, custom workers, and A2A peers have
different internal concepts for thread, checkpoint, tool loop, memory, pause, and result. Importing
any one framework's objects into AgentMesh business state would force every other runtime to mimic
that framework and would make control-plane history unstable when an SDK changes.

At the same time, AgentMesh contains flagship experiences such as Office and Music Studio. They are
useful product demonstrations but broaden the apparent core adoption surface and obscure the
reliability/governance boundary.

## Decision

AgentMesh is the framework-neutral control plane for production Agent work:

> Bring any Agent. Run it reliably. Govern every action. Observe and recover everything.

The architecture is divided into four planes:

1. **Control plane** owns Agent identity/version, Task/Run/Attempt, Assignment, scheduling, policy,
   Permit, budget, audit, and recovery.
2. **Runtime plane** executes an immutable Assignment through versioned adapters. A runtime owns its
   private cognition/checkpoint/workspace state but never owns AgentMesh business state.
3. **Capability plane** exposes governed MCP tools and immutable Capability Bundles to runtimes.
4. **Experience plane** provides optional business workflows and visualizations through public
   contracts and Runtime Extensions.

The following ownership rules are normative:

- PostgreSQL Task/Run/Attempt and governance records are the only business authority.
- A `Run` remains one logical execution trajectory. An `Attempt` remains one fenced control-plane
  lease. A new `RuntimeExecution` represents one provider-side execution and may be observed by a
  replacement Attempt only through an explicit fenced ownership transfer.
- Runtime checkpoints, remote task state, process state, container state, Redis, and telemetry are
  evidence/recovery inputs, never Task completion truth.
- The existing LangGraph implementation becomes a `ManagedAgentRuntime` adapter. LangGraph types do
  not appear in the canonical runtime contract.
- Runtime adapters receive bounded versioned DTOs and scoped capability handles. They do not receive
  an `ApplicationContainer`, Unit of Work, database URL, or owner repositories.
- Runtime output is a candidate observation. Only normal control-plane commands may transition a
  Run/Attempt/Task or consume governance authority.
- Irreversible actions use the shared Governed Action Protocol. Runtimes cannot mint approvals,
  policy decisions, or Permits.
- Office, Music Studio, Market Intelligence, and future scenarios depend on AgentMesh core; core
  must not import their implementations.

## Runtime/control-plane mapping

| Concept | Owner | Meaning |
|---|---|---|
| Agent Version | Agent Registry | Immutable identity, instructions, capability and runtime policy |
| Task/Subtask | Task Service | User-visible work and acceptance state |
| Run | Task Service | One logical execution trajectory |
| Attempt | Task Service/Orchestrator | One fenced lease allowed to advance a Run |
| Runtime Version | Agent Registry | Immutable adapter/provider descriptor and compatibility |
| Runtime Execution | Orchestrator | Binding from a Run to provider-side execution state |
| Runtime checkpoint/workspace | Runtime provider | Private recoverable execution state referenced opaquely |
| Governed Action | Policy/Governance | Exact proposed real-world side effect and authority chain |
| Artifact Version | Artifact Service | Immutable input/output/evidence content |

## Dependency direction

```text
Experience / Agent package
        ↓
Public Control API + Runtime/Capability SDK
        ↓
Application ports
        ↓
Domain contracts

Infrastructure adapters → application ports
Framework SDKs          → runtime adapter packages only
```

The domain and application contract packages must not import LangGraph, provider SDKs, FastAPI,
SQLAlchemy, Redis, or an experience namespace. CI architecture tests enforce this direction.

## Migration decision

The change uses expand/migrate/contract rather than a rewrite:

1. Add canonical runtime DTOs, ports, persistence, and conformance fixtures without changing the
   default path.
2. Wrap the existing LangGraph workflow behind the new adapter and dual-record runtime evidence.
3. Add a deterministic non-LangGraph reference adapter and require both to pass conformance.
4. Switch the Worker composition root to select by pinned Runtime Version.
5. Remove the legacy `WorkflowRunner`/`AgentExecutor` orchestration boundary only after parity,
   recovery, and rollback checks pass.

No migration changes historical Run identity. Existing active Runs either finish on the legacy path
or use an explicitly compatible LangGraph adapter version; they are never silently rebound.

## Consequences

### Positive

- LangGraph and other frameworks become ecosystem adapters rather than architectural competitors.
- Runtime failures and upgrades can be governed through one durable lifecycle.
- External Agents receive the same policy, budget, Artifact, identity, and audit semantics.
- Isolation and rollout groups can evolve behind provider ports without rewriting Task state.
- The public product boundary becomes smaller and easier to explain.

### Costs

- Runtime lifecycle and reconciliation become first-class persisted concepts.
- Adapter conformance, schema compatibility, and capability negotiation require ongoing maintenance.
- A synchronous in-process runtime and a remote asynchronous runtime need one carefully bounded
  observation model.
- Some existing code must temporarily support both legacy and new paths.

### Risks and mitigations

- **Thin-wrapper risk:** conformance must include a non-LangGraph process before claiming framework
  neutrality.
- **Second state-machine risk:** Runtime status cannot directly transition Task/Run; mapping occurs
  through explicit commands and guards.
- **Duplicate-execution risk:** dispatch keys, fenced ownership, provider inspection, and
  side-effect reconciliation precede redispatch.
- **Over-generalization risk:** v0.1 implements only capabilities required by LangGraph and one
  generic worker; optional capabilities remain explicit.

## Alternatives considered

- Continue with LangGraph as the universal runtime: rejected because other Agents would have to
  mimic LangGraph and the control-plane differentiation would remain weak.
- Replace LangGraph immediately: rejected because it already provides useful durable local
  execution and is a valid adapter.
- Treat A2A as the only runtime API: rejected because local/in-process/subprocess execution and
  provider lifecycle require semantics outside remote Agent interoperability.
- Make every runtime an extension loaded in the API process: rejected because runtime execution and
  business UI/service extension have different trust, isolation, and lifecycle needs.
- Rewrite the execution subsystem: rejected because existing Task/Run/Attempt and reliability
  semantics are valuable and migratable.

## Follow-up specifications

- [Managed Agent Runtime API v0.1](../architecture/modules/formal/managed-agent-runtime.md)
- [Governed Action Protocol v0.1](../architecture/modules/formal/governed-action-protocol.md)
- [Reliability Model and Chaos Qualification](../architecture/modules/formal/reliability-model-and-chaos.md)
- [P0 implementation plan](../architecture/control-plane-p0-implementation-plan.md)
