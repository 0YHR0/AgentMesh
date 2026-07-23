# AgentMesh v1 completion scope

Status: Accepted delivery scope
Last updated: 2026-07-23

This document separates the locally verifiable v1 product from integrations that require an
external identity provider, cloud secret store, remote A2A peer, or production cluster. A feature
is not reported as complete merely because an interface or proposal exists.

## Must ship in v1

- A runnable single-team control plane with direct, reviewed, and coordinated Task execution.
- Durable Task, Run, Attempt, Handoff, Plan Patch, Policy, Artifact, MCP, A2A, usage, and audit
  evidence.
- A Mission Map that exposes Agent state, governed interactions, deterministic replay, shared
  replay bookmarks, and cursor-paginated Task audit projections.
- Versioned compatibility fixtures for public contracts.
- Policy obligations and staged/quorum approval for high-risk actions.
- Local production baseline for Artifact storage and scanning, MCP health/circuit controls,
  Registry reconciliation, and bounded Runtime context assembly.
- Reproducible backup/restore, release checks, SLO definitions, and a system-level example.

## Extension-ready in v1

The following capabilities ship as documented interfaces, configuration boundaries, and safe
default-deny behavior. End-to-end certification requires infrastructure outside this repository:

- OIDC provider federation, OAuth token exchange, cloud secret providers, workload mTLS.
- Remote A2A streaming/push and remote Artifact transfer.
- Kubernetes high availability and managed PostgreSQL/Redis failover.
- Additional model providers and provider-specific streaming transports.

## Explicitly deferred

- Cross-tenant weighted fair dispatch and database RLS. The v1 release is single-team, while
  tenant identifiers remain in contracts to preserve an upgrade path. See
  [Cross-tenant fair dispatch](proposals/cross-tenant-fair-dispatch.md).
- Semantic clustering beyond the supported 20-Agent Task limit. Camera navigation, minimap,
  filtering, and deterministic replay cover the bounded v1 graph.
- Rollout groups that compare multiple candidate Runs for one work item. This remains a separate
  architecture proposal from coordinated execution.

## Completion rule

Each must-ship item needs a durable implementation, automated tests, operator documentation, and
an example or verification command. Extension-ready items must fail closed when unconfigured and
must not be described as fully implemented.
