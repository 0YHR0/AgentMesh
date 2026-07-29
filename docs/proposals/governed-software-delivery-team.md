# Governed software delivery team

Status: Proposed

## Outcome

AgentMesh should ship one credible end-to-end use case that proves why its execution runtime is
more valuable than a collection of Agent framework components. An operator gives an existing
repository a delivery objective, selects a bounded team template, and receives a reviewed,
tested, evidence-backed change on an isolated branch or draft pull request.

The operator should not have to manually coordinate prompts or copy messages between Agents. They
manage the objective, constraints, approval gates, budget, and exceptions while AgentMesh manages
durable ownership, dependencies, Handoffs, retries, review, and final evidence.

The first template is a **governed software delivery team**:

1. Product Agent clarifies the requested outcome and acceptance criteria.
2. Planner/Architect Agent produces the bounded implementation plan.
3. Implementer Agent changes code in an isolated workspace.
4. Reviewer Agent inspects the patch independently.
5. Tester Agent runs the authorized verification suite.
6. Supervisor Agent either accepts the evidence, requests a bounded revision, or escalates.

This is a product template over existing AgentMesh primitives, not a second execution engine.

## Why this use case

Software delivery combines the properties AgentMesh is intended to govern:

- multiple specialists with distinct capabilities and trust boundaries;
- a dependency graph whose ownership changes over time;
- expensive or unsafe operations requiring explicit authorization;
- durable intermediate evidence such as plans, patches, test reports, and review findings;
- failure, retry, reassignment, and human intervention;
- a clear final deliverable that can be independently inspected.

A basic framework can invoke several Agents. The differentiating proof for AgentMesh is that an
interrupted, revised, partially approved delivery remains understandable and recoverable without
inventing state in the UI or losing the identity of completed work.

## Core differentiation to demonstrate

The vertical slice is accepted only if it demonstrates all of the following:

| AgentMesh capability | Required proof |
|---|---|
| Durable execution | Restart API/Worker during the workflow and resume without repeating accepted side effects |
| Explicit ownership | Every active work item identifies its Agent Version, Run, Attempt, and role |
| Governed collaboration | Cross-role context uses dependency output or a structured Handoff, not hidden shared chat |
| Independent verification | Reviewer and Tester Runs are distinct from the Implementer Run |
| Safe tools | Repository reads/writes and external publication are MCP/Policy governed and audited |
| Human control | The operator can pause, reject, approve, cancel, or request a bounded revision |
| Evidence lineage | The final patch/PR links back to plan, implementation, tests, reviews, Tool calls, and approvals |
| Honest visualization | Console and Office project persisted state and never simulate unrecorded progress |

If the example only runs several prompts sequentially, it does not satisfy this proposal.

## Operator journey

### 1. Configure

The Console provides a guided setup surface:

- choose a repository workspace or approved Git provider binding;
- select model-backed Agent Versions for each role;
- bind SecretReferences without displaying or persisting raw credentials;
- select an allowed MCP Tool set;
- set token, cost, concurrency, revision, and deadline limits;
- choose whether draft-PR publication is allowed;
- run a preflight check before creating the Task.

Preflight reports each requirement as ready, unavailable, or blocked:

- database, worker, relay, and checkpoint readiness;
- model credential lease availability;
- repository access and clean isolated worktree creation;
- required Agent capabilities and published immutable versions;
- required MCP Tools and circuit state;
- Policy rules and required approver roles;
- available budget and concurrency quota.

The UI must not claim that an API key is valid merely because it is present. Validation uses a
bounded provider-specific check or remains explicitly unverified.

### 2. Launch

The operator enters:

- objective;
- repository and base revision;
- acceptance criteria;
- files or directories that are in or out of scope;
- verification commands from an allowlist;
- budget and deadline;
- optional implementation guidance.

AgentMesh snapshots a Goal Contract and a versioned team template, then creates a Coordinated Task.
Published Agent Version digests, Tool schema digests, repository base commit, and Policy version are
bound before execution.

### 3. Observe and intervene

The Mission Map and Office show:

- the current owner and status of each delivery stage;
- structured dependencies and Handoffs;
- repository/MCP activity using sanitized interaction events;
- review findings and failed verification as evidence, not fictional dialogue;
- approval gates for repository writes, network access, or PR publication;
- current budget, revision count, and deadline pressure.

The operator can inspect evidence, pause/cancel the Task, resolve an approval, or submit a Plan
Patch at an allowed barrier. Manual intervention must append evidence rather than mutate history.

### 4. Deliver

The final delivery bundle contains:

- repository base and resulting commit/patch digest;
- changed-file summary;
- acceptance criteria and result for each criterion;
- test commands, exit status, bounded logs, and report Artifacts;
- review findings and their disposition;
- Agent/Tool/Policy version identities;
- usage and cost settlement;
- unresolved risks;
- optional draft pull request URL.

Publication to GitHub or another external system is a separate governed action. The local patch
bundle remains available if publication is denied or fails with an unknown outcome.

## Team template

The initial template is versioned repository configuration rather than hard-coded orchestration:

| Role key | Responsibility | Required capabilities | Depends on |
|---|---|---|---|
| `product` | Normalize objective and acceptance criteria | `delivery.requirements` | — |
| `plan` | Inspect repository and produce an implementation plan | `repo.read`, `delivery.plan` | `product` |
| `implement` | Apply the approved bounded change | `repo.read`, `repo.write`, `delivery.implement` | `plan` |
| `review` | Independently review the patch | `repo.read`, `delivery.review` | `implement` |
| `test` | Run approved verification commands | `repo.read`, `test.execute` | `implement` |
| `supervise` | Join evidence and decide delivery/revision/escalation | `delivery.supervise` | `review`, `test` |

Reviewer and Tester must not silently reuse the Implementer's mutable conversation. They consume
the Goal Contract, immutable plan, patch Artifact, and bounded dependency evidence.

The template may bind multiple roles to one Agent Definition for a small installation, but each
role still receives a distinct Run and evidence boundary.

## Revision and recovery semantics

The first implementation uses the existing bounded reviewed-execution and Plan Patch barriers:

- review or test failure may request a new Implementer Run up to `max_revisions`;
- accepted completed work keeps its Run identity and output digest;
- a revision contains explicit findings and failed criteria;
- a Worker crash creates or reclaims an Attempt under existing fencing rules;
- a repository write of unknown outcome blocks automatic repetition until reconciliation;
- PR publication is idempotent under a stable operation key.

Active-Run supersession and compensation remain a separate runtime extension. This template must
fail closed instead of pretending that an arbitrary in-flight side effect can be rolled back.

## Workspace and Tool boundary

The delivery template never grants unrestricted host-shell access by default.

- Each Task uses an isolated worktree rooted at a server-authorized repository.
- Paths are resolved beneath the workspace root; symlink escapes are rejected.
- Read, search, patch, format, test, commit, and publication are distinct Tool capabilities.
- Command execution uses an administrator-defined argv allowlist and bounded time/output.
- Environment variables are assembled from metadata-safe configuration and short-lived credential
  leases; secrets are redacted from logs and Artifacts.
- Network access is denied unless an Agent Version, Tool, and Policy decision all allow it.
- Destructive filesystem operations and force-push are excluded from the first template.
- GitHub publication creates a draft pull request and requires a governed write Permit.

Codex, OpenCode, or another coding runtime can later be integrated as an external sandbox adapter.
Its brand name is not an Agent identity by itself: AgentMesh still binds a Definition, immutable
Version, role, capabilities, runtime policy, model/runtime configuration, and Tool allowlist.

## Configuration contract

The implementation should introduce a versioned `DeliveryTemplate` contract containing:

- template identity, semantic version, lifecycle, and content digest;
- role/DAG definitions and required capabilities;
- default Agent Version bindings with operator-overridable slots;
- required Tool capabilities and Policy action types;
- workspace policy reference;
- default budgets, deadlines, concurrency, and revision bounds;
- acceptance bundle schema version.

Task creation snapshots the resolved template digest. Editing the template affects future Tasks
only. The first release may check in one built-in template, but the runtime boundary must permit a
future Registry and UI editor.

## Feature gates

Recommended gates:

- `delivery_templates`: template inspection and preflight.
- `software_delivery`: Task creation from the governed software-delivery template.
- `git_publication`: external commit/push/draft-PR actions; off by default.

`software_delivery` depends on coordinated execution, Agent Registry management, Artifact storage,
activity projections, Policy/Approval, and the required MCP execution chain. The deterministic
fixture remains available without model credentials or external network access.

## Delivery slices

### Slice 1 — deterministic vertical slice

- add the versioned built-in team template;
- add template inspection, preflight, and Task creation APIs;
- add a deterministic repository fixture with a small failing test and expected patch;
- execute Product, Plan, Implement, Review, Test, and Supervisor Runs;
- produce a local evidence bundle without paid APIs or network access;
- expose the workflow in the Admin Console and Office.

This slice proves orchestration and evidence contracts in CI.

### Slice 2 — model-backed local delivery

- bind real model-backed Agent Versions through SecretReferences;
- add confined repository read/patch/test MCP capabilities;
- stream operator-visible progress without exposing chain-of-thought;
- support bounded revision after review or test failure;
- verify Worker restart and Attempt recovery against a real temporary repository.

### Slice 3 — governed GitHub delivery

- bind a GitHub installation or credential reference;
- create an isolated branch and idempotent draft pull request;
- require Policy approval for publication;
- reconcile unknown publication outcomes;
- attach the sanitized AgentMesh evidence summary to the PR without exposing secrets or raw model
  reasoning.

### Slice 4 — reusable templates

- add Delivery Template Registry lifecycle and immutable versions;
- provide a Console builder for role slots, capabilities, DAG, budgets, and Policy requirements;
- allow additional bounded workflows such as research delivery, incident response, and content
  review without changing the execution engine.

## Acceptance criteria

- A new operator can configure and preflight the deterministic example from the Console.
- CI runs the complete six-role workflow without paid APIs or external network calls.
- A model-backed example requires only explicitly referenced credentials and an approved workspace.
- Killing a Worker during implementation does not duplicate an accepted repository side effect.
- Review and test failures produce bounded revision evidence and never overwrite the previous Run.
- The final evidence bundle identifies every Agent Version, Tool schema, Policy decision, Artifact,
  repository revision, test result, and settled usage record.
- No raw credentials, hidden reasoning, unrestricted command output, or unsanitized Tool payloads
  appear in Console, Office, logs, exports, or PR content.
- `prefers-reduced-motion` and the non-3D Console retain the full operational workflow.
- Disabling the feature gates leaves the existing single-team v1 behavior unchanged.

## Success measures

Initial measures are operational rather than vanity metrics:

- median time from clean installation to successful deterministic delivery;
- percentage of preflight failures that identify a concrete remediation;
- successful recovery rate after injected Worker interruption;
- percentage of review/test failures resolved within the configured revision bound;
- count of unreconciled unknown Tool/publication outcomes;
- completeness of evidence bundles against the acceptance schema;
- operator interventions per successful delivery.

The project should not optimize the number of animated Agents or messages exchanged. Fewer,
better-governed interactions are preferable.

## Non-goals

- autonomous production deployment;
- unrestricted shell or arbitrary host access;
- replacing GitHub Actions or repository-native CI;
- hidden Agent debate or chain-of-thought visualization;
- cross-tenant scheduling;
- rollout groups that run competing candidate implementations;
- automatic rollback of irreversible external side effects;
- claiming support for every coding CLI or model provider in the initial slice.

## Open decisions

Before Slice 2 implementation:

1. Whether the first writable workspace Tool extends the existing MCP workspace server or uses a
   dedicated repository Tool server.
2. The minimal portable patch Artifact format and maximum bounded diff size.
3. Which verification commands are safe enough for the built-in allowlist.
4. Whether GitHub publication is implemented through MCP or a dedicated external adapter while
   preserving the same GovernedAction and idempotency semantics.
5. The exact barrier at which review/test findings can produce a Plan Patch versus a bounded
   revision of the existing implementation node.

These decisions require L2 design before production code. They do not block Slice 1's deterministic
fixture and evidence contract.
