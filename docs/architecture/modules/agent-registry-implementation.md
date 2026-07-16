# Agent Registry implementation

Status: Accepted for implementation increment
Owners: AgentMesh maintainers
Depends on: [Agent Registry formal L2](formal/agent-registry.md)

## 1. Scope

This increment implements the local Agent Registry core required before multi-Agent
scheduling. It separates stable Agent identity, immutable published configuration,
capability contracts, deployments, and live instances. A2A Agent Card import remains in
the A2A implementation increment, while authorization and signed supply-chain evidence
remain dependent on Identity/Policy/Artifact modules.

## 2. Implemented entity boundaries

| Entity | Implemented responsibility |
|---|---|
| AgentDefinition | tenant-scoped stable name, owner, visibility, tags, lifecycle, default Version |
| AgentVersion | SemVer draft/review/publication lifecycle and immutable content digest |
| Capability | namespaced key, SemVer contract, JSON schemas, verification evidence requirements |
| AgentDeployment | Version/environment/runtime binding, desired/current status, traffic and rollout metadata |
| AgentInstance | trusted heartbeat snapshot, health, capacity, endpoint and monotonic lease epoch |

Published configuration fields are inserted once and repository updates only lifecycle,
verification, digest, and revocation fields. Fixes therefore require a new Version.

## 3. Lifecycle

```text
DRAFT -> IN_REVIEW -> PUBLISHED -> DEPRECATED -> RETIRED
                  \-> REJECTED
PUBLISHED | DEPRECATED -> REVOKED
```

Only `PUBLISHED` Versions can become a Definition default, be selected as new candidates,
or create a Deployment. Deprecation, retirement, revocation, and Definition archival clear
an affected default. Revocation stores a reason and exposes active Runs bound to the Version.

## 4. Run binding

When a Run is accepted, the same PostgreSQL transaction locks the configured Agent
Definition and its default Version. The Run persists:

- stable Agent name;
- immutable Agent Version UUID;
- immutable `sha256:` content digest.

Changing the default later does not rewrite queued, running, or historical Runs. A revoked
Version cannot be selected for a new Run, while affected active Runs remain queryable for
operator or future Policy action.

## 5. Capability discovery

Candidate search accepts verified capability keys and an optional execution mode. It only
returns published compatible Versions. Active Deployment snapshots are attached when
available; capacity is a scheduling hint and does not reserve a slot.

Declared and verified capabilities are separate fields. Publication rejects verified keys
that were not declared or are absent from the tenant Capability catalog.

## 6. HTTP surface

The `/api/v1` registry endpoints support:

- create/list/get/archive Agent Definitions;
- create, submit, reject, publish, deprecate, retire, and revoke Versions;
- switch a published default Version;
- create/list Capability contracts and search candidates;
- create/list/update Deployments;
- trusted internal Instance heartbeat and Instance listing;
- list active Runs affected by a Version.

The current tenant comes from application configuration until the Identity module supplies
authenticated tenant context.

## 7. Consistency and events

- tenant/name and capability key/version creation use PostgreSQL advisory locks;
- Definition optimistic versions protect default and lifecycle changes;
- lifecycle changes and Outbox Events commit in the same transaction;
- Run binding locks the selected published Version;
- registry events route to `agentmesh.domain-events`, not the execution command stream;
- the built-in deterministic Agent and `general.task` capability are bootstrapped
  idempotently by a separate seed deployment step for a configured tenant.

## 8. Deferred dependency-bound work

- A2A Agent Card snapshot/import, signatures, ETag refresh and SSRF controls;
- author/reviewer/deployer/operator RBAC and workload-authenticated heartbeat;
- Artifact-backed prompt/schema/runtime packages and signature verification;
- evaluation evidence ingestion and capability-specific probes;
- cache invalidation, semantic search, retention and high-scale heartbeat storage.

These are not hidden inside the registry core; they will arrive through the documented A2A,
Identity, Policy, Artifact, Observability, and Scheduler boundaries.

## 9. Verified acceptance criteria

- published Versions have deterministic content digests and cannot be republished;
- declared and verified capabilities remain distinct;
- duplicate names and capability versions are rejected per tenant;
- default switches do not alter existing Run bindings;
- revoked Versions disappear from candidate search and expose affected active Runs;
- Deployment/Instance status and lease epoch rules are tested;
- the real PostgreSQL/Redis execution test completes using a persisted Agent Version.
