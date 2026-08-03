# AgentMesh

[English](README.md) | [简体中文](README.zh-CN.md)

[![CI](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/ci.yml)
[![CodeQL](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml/badge.svg)](https://github.com/0YHR0/AgentMesh/actions/workflows/codeql.yml)

AgentMesh is an open-source control plane for coordinating, observing, and governing teams of AI agents.

Define the goal, constraints, and acceptance criteria. AgentMesh plans, assigns, routes, observes,
governs, and audits the work performed by a team of specialized agents.

> Status: Alpha (`v0.1.0-alpha.1`). The supported single-team v1 baseline is
> implementation-complete and release-qualified. This release is intended for evaluation,
> local development, and non-critical single-team deployments; multi-tenant isolation and
> production HA certification remain post-v1 work.

## Vision

AgentMesh is designed as a self-hostable, framework-neutral multi-agent platform:

- Simple tasks stay with one agent and avoid unnecessary coordination overhead.
- Complex work can be decomposed, parallelized, reviewed, revised, and human-approved.
- Each agent can have a distinct role, model, tools, knowledge, permissions, and resource quota.
- Local and remote agents share consistent Task, Handoff, and Artifact semantics.
- State changes, calls, cost, quality evidence, and operator actions remain observable and auditable.
- Optional Company finance controls keep estimated pipeline, verified cash, settled cost, and
  hierarchical budget reservations evidence-classified and auditable.
- Open protocols and private deployment are first-class design constraints.

## Proposed stack

- Orchestration: LangGraph
- System of record: PostgreSQL
- Agent interoperability: A2A
- Tool and context interoperability: MCP
- LLM observability and evaluation: Langfuse
- Event delivery: Redis Streams initially, with an abstraction for NATS JetStream
- Artifact storage: content-addressed local storage in v1, with an S3-compatible adapter boundary

The stack is an architecture baseline rather than a permanent product boundary. Material
decisions are recorded as ADRs.

## Architecture documentation

- [Documentation map](docs/README.md)
- [Architecture levels](docs/architecture/README.md)
- [L0 system design](docs/architecture/L0-system-design.md)
- [L1 design plan](docs/architecture/L1-design-plan.md)
- [Formal L2 design baseline](docs/architecture/modules/formal/README.md)
- [Implementation status](docs/implementation-status.md)
- [v1 completion scope](docs/v1-completion-scope.md)
- [Roadmap](docs/roadmap.md)
- [Changelog](CHANGELOG.md)
- [Glossary](docs/glossary.md)
- [Architecture decisions](docs/adr/README.md)
- [CI and pull request governance](docs/architecture/modules/ci-and-pr-governance.md)

## Runnable asynchronous slice

The current implementation proves this path:

```text
HTTP task command (202 Accepted)
  -> Task + Run + Transactional Outbox in PostgreSQL
  -> Event Relay -> Redis Streams consumer group
  -> Execution Worker + Attempt lease/fencing token
  -> LangGraph workflow + optional allowlisted read-only MCP Tool
  -> PostgreSQL checkpoint
  -> Inbox deduplication + persisted business result and usage ledger
  -> optional privacy-safe Langfuse Attempt Trace
```

The API, Event Relay, and Worker are separate processes. Redis is delivery infrastructure,
while PostgreSQL remains the business source of truth. The deterministic executor
intentionally requires no model API key.

The Relay also performs bounded Outbox/Inbox cleanup and pending-safe Redis Stream retention.
Compose exposes its Prometheus metrics at `http://localhost:9464/metrics`. The default Inbox
deduplication horizon is 30 days; retention is part of the reliable core and is not a feature
gate. See [Messaging retention and cleanup](docs/architecture/modules/messaging-retention-implementation.md)
for safety guarantees and tuning.

### Feature profiles

AgentMesh defaults to the `minimal` profile so a first-time user only needs the Task API and
the built-in deterministic Agent. Optional management APIs are enabled explicitly:

| Profile | Enabled optional capabilities |
|---|---|
| `minimal` | None; core task execution remains available |
| `standard` | Reviewed execution, Agent Registry management, and human Task resolution |
| `full` | Standard plus coordinated DAG/Handoffs, Deployments, inline-small Artifacts, read-only MCP, observability, and Task budgets; identity remains explicit opt-in |

Choose a profile in `.env` before starting Compose:

```dotenv
AGENTMESH_FEATURE_PROFILE=standard
```

Individual gates can override the profile:

```dotenv
AGENTMESH_FEATURE_GATES=reviewed_execution=true,coordinated_execution=true,dynamic_replanning=true,handoffs=true,agent_registry_management=true,artifact_service=true,mcp_read_tools=true,observability=true,budget_admission=true,human_resolution=true
```

Configuration is validated at startup and changes require a restart. Dependencies are strict:
`agent_deployments` requires `agent_registry_management`. Query `GET /api/v1/features` to inspect
the effective state. Disabled server-side APIs return `403` with code `feature_disabled`.
See the [Feature Gate module design](docs/architecture/modules/feature-gates.md) for the extension
contract and boundaries.

The first Virtual Company module is available as an explicit opt-in:

```dotenv
AGENTMESH_FEATURE_PROFILE=full
AGENTMESH_FEATURE_GATES=company_model=true
```

It adds tenant-scoped Company, Organization Unit, Position, Appointment, and organization-graph
APIs under `/api/v1/companies`. Only published Agent Versions satisfying a Position's required
capabilities can be appointed. The Office uses active Appointments and matching organization-unit
spaces when the gate is enabled; with the gate disabled, the existing Agent Team runtime is
unchanged.

Add `company_goals=true` to enable Operating Cycles, Objectives, verified-versus-estimated Key
Results, Initiatives, and Initiative-launched Task lineage. Goal APIs remain under the owning
Company path. An Initiative must pass explicit approval and activation transitions before it can
create a Task through the normal Task application service; completing an Initiative requires at
least one durable Task link. The Office projects active Objective and Initiative counts onto
matching organization-unit spaces.

Add `company_operations=true` to turn approved recurring or external-event work into idempotent,
traceable Tasks:

```dotenv
AGENTMESH_FEATURE_PROFILE=full
AGENTMESH_FEATURE_GATES=company_model=true,company_goals=true,company_operations=true
```

Operations support deterministic interval and manual/business-event triggers, bounded catch-up,
run-window and concurrency admission, stable occurrence keys, and operator-visible dispatch
exceptions. Start continuous scheduling with `agentmesh-company-operations`, or with
`docker compose --profile company up`. See the
[Company Operations implementation](docs/architecture/modules/company-operations-implementation.md).

Typed business records are independently available without enabling the scheduler:

```dotenv
AGENTMESH_FEATURE_PROFILE=full
AGENTMESH_FEATURE_GATES=company_model=true,business_objects=true
```

The `business_objects` module provides versioned JSON Schema Types, declared lifecycle actions,
optimistic concurrency, append-only revisions, evidence references, and sensitive-field redaction.
It rejects arbitrary patches and external-side-effect actions. See the
[Typed Business Objects implementation](docs/architecture/modules/business-objects-implementation.md).

Governed long-term Company memory is another independent opt-in:

```dotenv
AGENTMESH_FEATURE_PROFILE=full
AGENTMESH_FEATURE_GATES=company_model=true,organizational_memory=true
```

The Memory Service provides versioned namespace policies, candidate/review lifecycle, immutable
provenance and evidence, supersession/revocation/expiry, exact bounded retrieval, conflict markers,
automatic executor-Run context injection, and structured post-Task candidate capture without
requiring embeddings or an external API key. PostgreSQL remains authoritative; optional semantic
systems such as Mem0 or MemOS belong behind the ranking adapter boundary and may be selected by a
deployment without receiving policy or audit authority. See the
[Organizational Memory implementation](docs/architecture/modules/organizational-memory-implementation.md).

Company finance is opt-in and does not enable payments or external commercial writes:

```dotenv
AGENTMESH_FEATURE_PROFILE=full
AGENTMESH_FEATURE_GATES=company_model=true,company_finance_read=true,financial_governance=true
```

It provides hierarchical single-currency allocations, append-only reserve/release/settlement
entries, immutable classified economic evidence, separation-of-duties expense review, and an
owner dashboard under `/api/v1/companies/{company_id}/finance`. See the
[Financial Governance implementation](docs/architecture/modules/financial-governance-implementation.md).

Reusable declarative Company Packs are another explicit opt-in:

```dotenv
AGENTMESH_FEATURE_PROFILE=full
AGENTMESH_FEATURE_GATES=company_model=true,business_objects=true,company_packs=true
```

The Pack API supports validation, publication, dependency/Feature preview, digest-pinned atomic
installation, and an audit ledger for Organization Units, Positions, Business Object Types,
Operating Cycles, Objectives/KRs, Initiatives, Operations, Memory Policies, and Budget
Allocations.
See the [Company Packs implementation](docs/architecture/modules/company-packs-implementation.md).

The first end-to-end product is **Music Studio**. With the same Pack gates (and optional 2.5D
Office), start the stack and open `http://localhost:8000/music-studio`:

```bash
AGENTMESH_FEATURE_PROFILE=full \
AGENTMESH_FEATURE_GATES=company_model=true,business_objects=true,company_packs=true,office_3d=true \
docker compose up -d --build
```

The guided Demo installs a five-department studio, appoints six specialist employees, runs a
six-stage coordinated project, and returns two playable deterministic WAV candidates with measured
evidence in every round. The owner can compare and explicitly select a candidate, request bounded
revisions, approve it, and download an immutable ZIP containing the selected audio, lyrics, rights
manifest, and SHA-256-linked release manifest. It uses no model or music API key and makes no
external request. The focused workspace defaults to English and can switch to Chinese; low-level
Tasks, Runs, Artifacts, and governance records remain in the Admin Console.

Music Studio is loaded through the trusted in-process Runtime Extension API rather than imported
by the core application. `AGENTMESH_RUNTIME_EXTENSIONS=agentmesh.music-studio` is the default;
set it to an empty value to disable every installed extension. Inspect effective versions,
required Features/Credentials, permissions, workspaces, and health at `GET /api/v1/extensions`.
Third-party trusted Python packages can publish the `agentmesh.runtime_extensions` entry-point
group. See the [Runtime Extension Protocol](docs/architecture/modules/runtime-extension-protocol.md).
The independent
[AgentMesh Extension Starter](https://github.com/0YHR0/AgentMesh-Extension-Starter) provides a
tested Daily Brief scenario and a minimal repository template for extension authors.

The same gates expose the built-in **Market Intelligence Studio** in the Admin Console's
**Company** tab. Previewing it shows all 32 resource mutations, permissions, credentials, and the
external-write boundary. One click creates the Company, eight departments, 17 Positions, seven
published Business Object Types, configuration, and installation evidence in one transaction.
No model API key is required. Run the checked-in
[offline evidence-chain example](examples/market-intelligence-studio/README.md) before binding
real Agents or research tools.

After creating the Studio, its separately gated **Operations Pack** can be previewed and enabled
from the same page. Enable the additional domain gates:

```dotenv
AGENTMESH_FEATURE_PROFILE=full
AGENTMESH_FEATURE_GATES=company_model=true,company_goals=true,company_operations=true,business_objects=true,organizational_memory=true,company_finance_read=true,financial_governance=true,company_packs=true
```

The activation transaction creates a 28-day Operating Cycle, one active Objective, four KRs, an
active Initiative, an initial budget boundary, a conservative Memory Policy, and three recurring
Operations. The Operations remain `DRAFT`: activation never starts Agents, enables external
writes, or bypasses approval.

The Company page now continues with an explicit workforce wizard. Create and publish Agent
Versions in **Agent Registry** with the capabilities shown for each Position, then:

1. select a capability-qualified Agent for each operating Position and save the Appointments;
2. inspect the per-Operation staffing preflight;
3. select the ready Operations and start them explicitly.

An Appointment records the immutable Agent Version. Starting an Operation fails atomically if any
bound Position is unstaffed, its Agent is inactive, its appointed Version is no longer the
published default, or its verified capabilities no longer satisfy the Position. Each occurrence
then creates a normal `COORDINATED` Task with one attributable Subtask per appointed Position,
including Appointment and Agent-Version evidence in the Task context. It still does not grant
external-write authority or bypass the normal Task run action and policy controls.

The Company page also provides a **Live Research Control** once the Studio exists. Its preflight
fails closed until `web.search` and `source.read` resolve to unique, published, read-only MCP
bindings and the Research Lead, Research Specialist, Fact Reviewer, and Editorial Reviewer
Positions have ready Appointments. The two research Agents must explicitly allow both tools in
their immutable Agent Version tool profiles. A successful launch persists a Research Question
and starts one observable five-stage coordinated Task:

```text
scope plan -> evidence collection -> claim synthesis -> fact check -> internal report draft
```

This workflow is provider-neutral: any MCP server can supply the logical tools, and credentials
remain behind the Credential Broker when the server requires authentication. It never publishes
or delivers externally. After completion, AgentMesh validates the final evidence bundle against
successful MCP Tool Invocation IDs and idempotently materializes draft Source Records, Claim
Registers, an internal report Artifact, and a draft Research Report. Invalid or incomplete bundles
fail closed without changing the Task result; operators can inspect status and explicitly retry:

```text
GET  /api/v1/company-templates/market-intelligence-studio/research/tasks/{task_id}/materialization
POST /api/v1/company-templates/market-intelligence-studio/research/tasks/{task_id}/materialize
```

All Business Objects remain in their normal draft lifecycle and still require the configured
human review/approval actions. See the
[Market Intelligence Studio example](examples/market-intelligence-studio/README.md) for the
configuration contract and launch API.

With the `standard` profile, a Task can request independent review using structured acceptance
criteria. Executor and Reviewer work is persisted as separate Runs, failed reviews create bounded
revision Runs, and exhausted limits move the Task to `WAITING_APPROVAL` instead of accepting a
failed candidate. See the
[Reviewed execution implementation](docs/architecture/modules/reviewed-execution-implementation.md).

With the `full` profile, distinct Subtasks can run in parallel and flow their durable outputs into
dependent Subtasks before an independent Supervisor synthesizes the final result:

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Research and summarize","execution_mode":"COORDINATED","max_concurrency":2,"subtasks":[{"key":"research-a","objective":"Research source A"},{"key":"research-b","objective":"Research source B"},{"key":"synthesize","objective":"Compare the research","depends_on":["research-a","research-b"]}]}'
```

Run the returned Task normally and inspect its `subtasks`, Runs, and Attempts through the Task API.
See [Coordinated Subtask DAG execution](docs/architecture/modules/coordinated-dag-implementation.md)
for durability, capability matching, propagation, and current-scope guarantees.

In the `full` profile, a completed source Subtask can also request a structured Handoff to an
unstarted downstream Subtask. The target Agent explicitly accepts or rejects it through the Task
Handoff endpoints. Accepted contracts bind the later target Run and enter its structured context;
rejected contracts remain audit history. See the
[Handoff lifecycle implementation](docs/architecture/modules/handoff-lifecycle-implementation.md).

The Artifact service accepts Base64-encoded UTF-8 `text/plain` and `application/json`. Content up
to 64 KiB remains inline by default; larger content (up to 10 MiB by default) is stored in a
content-addressed local blob directory. Every download revalidates SHA-256, and the durable
Version records storage and scan status. Cloud object storage, DLP, and malware-engine adapters
remain external-infrastructure extensions.

### Run with Docker Compose

```bash
docker compose up --build
```

Open the AgentMesh Console at `http://localhost:8000`. The first-run Compose configuration keeps
the minimal feature profile and enables only coordinated execution, so the Console can create a
real multi-Agent Subtask DAG without enabling the advanced governance surfaces. The Console uses
the same Control API as external clients and shows the authoritative Task, Subtask, Run, status,
dependency, assignment, and output projections. Its Mission Map renders Agent stations, DAG routes,
and redacted persisted Handoff/MCP/A2A/approval/Plan Patch interactions with external Tool, peer,
gate, and patch nodes. Operators can inspect each work unit, while animation is driven only by
durable events. A stable event-time scrubber can pause live mode, step through persisted events,
project the Run/Subtask state at that position, save PostgreSQL-backed shared bookmarks, and export a sanitized
`agentmesh.mission-replay.v1` JSON evidence bundle. Wide and deep DAGs can be zoomed, dragged,
fit to the viewport, reset to one-to-one scale, focused on the selected Agent, and navigated through
a clickable overview minimap. The original work-card view remains available as a low-motion
alternative. It polls every three seconds and provides run, pause, resume, and cancel controls.
The Console defaults to English. Use the language control in the top bar to switch to Simplified
Chinese; the choice is saved in the browser.

Open `http://localhost:8000/world`, or use **AgentMesh Office** in the Console top bar, for the
spatial company view. Its central scene is rendered by the self-hosted Phaser 3.90 runtime while
task lists and inspectors remain accessible HTML. The office is a bounded multi-screen map with
WASD/arrow-key and drag panning, wheel/HUD zoom, selected-employee focus, and a clickable minimap.
Its checked-in semantic map drives department views and bounded A* corridor routing. It also
supports a roster selector, reduced-motion Handoffs, optional ambient sound, four-direction
employees, and density clusters above 50 visible employees. Published Agent Definitions become
employees; runtime-only Agent IDs are projected from real Task Runs. Departments derive from role,
capability, and tag metadata. Employee bubbles, collaboration routes, moving packets, and walking
Handoff animations are projections of authoritative Task, Run, Subtask, and Handoff state—not a
separate simulation or fictional experience-level system. The page uses the same session-scoped
Bearer token and English/Chinese preference as the main Console.

For an optional high-DPI orthographic strategy view, explicitly enable
`AGENTMESH_FEATURE_GATES=office_3d=true` and open `http://localhost:8000/world-3d`. This
self-hosted Babylon.js renderer uses 3D scene geometry and crisp DOM status labels while preserving
`/world` as the lightweight fallback. Research, Analysis, Engineering, and Operations have distinct
building silhouettes, functional equipment, bilingual plaques, and restrained signature motion
instead of color-only theming. The experimental renderer is excluded from every built-in profile,
including `full`, until explicitly enabled.

When enabled, `/world-3d` is the primary daily company interface and `/` is the **Admin Console**.
Operators can create and optionally start real direct or coordinated Tasks without leaving the
Office. The default campus contains eight independently styled spaces on an authoritative grid.
Employee drops snap to unoccupied cells and persist in PostgreSQL; crossing a room boundary changes
the employee's department as derived by the server. Idle employees take short rendering-only walks
inside their department without changing their persisted workstation or Task state. Persisted
Handoffs use bounded A* routes around employees and server-declared furniture, with an illuminated
grid trail while the source Agent walks to the target. Agents waiting for approval visit Operations;
working and blocked Agents remain at their station with distinct, truthful poses. These movements
are projections only and never advance Task state. The Campus Planner can also add up to eight
tenant-shared decorative spaces with automatically expanding bounds, roads, labels, camera limits,
and navigation. Their bounded definitions are persisted in PostgreSQL and synchronized across
browser sessions; a one-time compatibility path imports an existing browser-local layout. The
Office also projects sanitized MCP, A2A, and approval interactions as short-lived data packets
between Agents and the relevant governed station. Task and Agent truth remains in the Control API.

With `mcp_read_tools` enabled, the Console also exposes a searchable Tool Catalog and the Agent
Version builder offers published read-only Tools as explicit checkboxes. With the governed MCP
dependency chain enabled, authorized Tool Providers can search the official MCP Registry, perform
bounded anonymous discovery, import explicitly read-only Tool schemas, and publish the immutable
snapshots without hand-writing JSON. Registry entries are candidates, not trust assertions; bearer
and custom-auth bootstrap remain manual.

To see every governed route on one Task without paid APIs or external network calls, enable the
`full` feature profile and create the opt-in research-brief showcase:

```bash
AGENTMESH_FEATURE_PROFILE=full docker compose up -d
docker compose --profile showcase run --rm showcase
```

PowerShell users can set `$env:AGENTMESH_FEATURE_PROFILE="full"` before `docker compose up -d`.
Select the Task whose title starts with `[Showcase]`; its Mission Map contains retry evidence,
Handoff, MCP, A2A, approval, and Plan Patch records, with filters for transport, Agent, status,
event kind, and trace. See [the showcase guide](examples/research-brief/README.md).

For a small remote test host (2 vCPU / 4 GiB RAM), use the resource-bounded overlay:

```bash
AGENTMESH_FEATURE_PROFILE=full \
docker compose -f compose.yaml -f compose.test.yaml up -d --build
```

The overlay binds the API and Relay metrics to loopback and assigns conservative container memory
limits. Reach the Console through an SSH tunnel instead of exposing the unauthenticated development
profile to the public internet:

```bash
ssh -L 8000:127.0.0.1:8000 user@test-host
```

The interface has no separate frontend build or service. If Identity/RBAC is enabled, use
**Connection settings** to provide a Bearer token; the token is retained only in browser session
storage. Open the API documentation at `http://localhost:8000/docs`, or run:

The default team uses three distinct published Agent Versions: `demo-researcher`, `demo-analyst`,
and `demo-synthesizer`. By default they use the deterministic runtime and require no API key. To
run the same version-bound roles through the OpenAI Responses API, copy `.env.example` to `.env`
and set these local values before starting Compose:

```dotenv
AGENTMESH_MODEL_PROVIDER=openai
AGENTMESH_MODEL_NAME=gpt-5.6-terra
AGENTMESH_MODEL_REASONING_EFFORT=low
OPENAI_API_KEY=replace-with-your-local-key
```

Do not commit `.env`. The Worker reads the key from its environment; AgentMesh does not store it in
PostgreSQL or expose it to the Console. Remove the model settings or restore
`AGENTMESH_MODEL_PROVIDER=deterministic` to return to the free local demonstration. See the
[role-bound model runtime](docs/architecture/modules/role-bound-model-runtime-implementation.md)
for the execution and trust boundaries.

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Run the AgentMesh demo","input":{"source":"curl"}}'
```

Use the returned task ID to execute it:

```bash
curl -i -X POST http://localhost:8000/api/v1/tasks/<task-id>/runs \
  -H "Idempotency-Key: example-run-1"
```

The run command returns `202 Accepted`. Query `GET /api/v1/tasks/<task-id>` to observe
the Task, Run, and Attempt states until completion.

Pause queued or running work and later resume the same durable Run and LangGraph thread:

```bash
curl -i -X POST http://localhost:8000/api/v1/tasks/<task-id>/pause
curl -i -X POST http://localhost:8000/api/v1/tasks/<task-id>/resume
```

A queued Run pauses immediately. A running Run first reports `PAUSE_REQUESTED` and becomes
`PAUSED` at the next durable post-node boundary. Resume creates a new fenced Attempt without
re-executing a node whose output is already checkpointed.

Enable the `full` profile to invoke the bundled read-only MCP workspace Tool. In the Compose image,
the allowed root defaults to `/app`; configure `AGENTMESH_MCP_WORKSPACE_ROOT` and mount a volume to
expose a different directory.

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"objective":"Read the project README","input":{"tool_call":{"tool":"workspace.read_text","arguments":{"path":"README.md"}}}}'
```

Run the returned Task normally, then inspect its digest-only invocation audit at
`GET /api/v1/tasks/<task-id>/tool-invocations`. The runtime verifies the MCP Server identity,
Tool allowlist, `readOnlyHint`, JSON Schema, path confinement, and result byte limit.

Enable the `observability` Gate to expose `GET /api/v1/tasks/<task-id>/usage`. Each Attempt includes
a stable Trace ID. Model executors can report Token buckets and integer-micro costs into the
PostgreSQL business ledger; the built-in deterministic Agent reports no fabricated usage.

To mirror content-free Attempt and generation metadata to Langfuse, set:

```dotenv
AGENTMESH_FEATURE_GATES=observability=true
AGENTMESH_LANGFUSE_ENABLED=true
AGENTMESH_LANGFUSE_PUBLIC_KEY=pk-lf-...
AGENTMESH_LANGFUSE_SECRET_KEY=sk-lf-...
AGENTMESH_LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

Task objective, input/output, prompts, and Tool bodies are not exported by this adapter. Langfuse
failure does not affect execution or accounting. See the
[Observability and usage increment](docs/architecture/modules/observability-usage-implementation.md).

Enable `observability` and `budget_admission` to attach an immutable Task budget covering Run and
Attempt counts, Token/cost totals, and an overall UTC deadline. Token/cost limits include explicit
per-Attempt reservations, preventing parallel Workers from spending the same remaining capacity.
Inspect authoritative settled and reserved values at `GET /api/v1/tasks/<task-id>/budget`.

```json
{"objective":"Bounded work","budget":{"max_runs":3,"max_attempts":4,"max_tokens":20000,"token_reservation_per_attempt":4000,"max_cost_micros":5000000,"cost_reservation_micros_per_attempt":1000000,"currency":"USD"}}
```

Actual overruns and expired deadlines preserve accounting and move the Task to
`WAITING_APPROVAL`. An operator can inspect the durable candidate, reject it, or submit a monotonic
budget increase and resume from the recorded execution boundary:

```bash
curl -X POST http://localhost:8000/api/v1/tasks/<task-id>/resolutions/increase-budget-and-resume \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: increase-budget-1" \
  -d '{"actor":"operator","reason":"Approved extension","budget":{"max_runs":5}}'
```

See the [Task budget](docs/architecture/modules/task-budget-admission-implementation.md) and
[Human Task resolution](docs/architecture/modules/human-task-resolution-implementation.md)
implementation documents. With `identity_rbac` enabled, the authenticated Principal replaces the
client-supplied audit actor.

### Enable the Identity and RBAC boundary

Identity is disabled in every built-in profile, including `full`, because enabling it safely
requires an explicit credential. Generate a long random Bearer token outside the repository and
configure only its SHA-256 digest:

```bash
python -c "import hashlib; print(hashlib.sha256(b'replace-with-a-random-token-at-least-32-bytes').hexdigest())"
```

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true
AGENTMESH_IDENTITY_PRINCIPALS_JSON=[{"principal_id":"admin","tenant_id":"default","principal_type":"USER","status":"ACTIVE","roles":["TENANT_ADMIN"],"token_sha256":"<sha256-hex>"}]
```

After restarting, all `/api/v1` requests require the Bearer token. Health, readiness, OpenAPI, and
API documentation remain public.

```bash
curl http://localhost:8000/api/v1/identity/me \
  -H "Authorization: Bearer <raw-token>"
```

Available baseline roles are `TENANT_ADMIN`, `OPERATOR`, `AGENT_AUTHOR`, `AGENT_PUBLISHER`,
`TOOL_PROVIDER`, `APPROVER`, and `AUDITOR`. Agent authors cannot publish their own Versions. See the
[Identity/RBAC baseline](docs/architecture/modules/identity-rbac-baseline-implementation.md) for
the permission matrix, failure behavior, and current limitations.

For durable Principal and RoleBinding administration, enable `persistent_identity` as well. In
this mode configured bootstrap Principal IDs must be UUIDs. Initial roles are seeded only when a
Principal is first created, so a later database revocation is never undone by restart.

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true,persistent_identity=true
AGENTMESH_IDENTITY_PRINCIPALS_JSON=[{"principal_id":"10000000-0000-0000-0000-000000000001","tenant_id":"default","principal_type":"USER","status":"ACTIVE","roles":["TENANT_ADMIN"],"token_sha256":"<sha256-hex>"}]
AGENTMESH_IDENTITY_OIDC_ISSUER=https://idp.example
AGENTMESH_IDENTITY_OIDC_AUDIENCE=agentmesh-api
```

OIDC tokens must pass signature, issuer, audience and time validation and match a registered
ExternalIdentity. AgentMesh ignores IdP role claims and resolves active PostgreSQL RoleBindings on
every request. Administration under `/api/v1/identity/principals` requires `TENANT_ADMIN`. This
Gate remains disabled in all built-in profiles. See the
[Persistent Identity/OIDC baseline](docs/architecture/modules/persistent-identity-oidc-implementation.md).

### Require Policy approval for high-risk actions

Enable Policy only together with Identity. The built-in secure rules require independent approval
for Agent Version publication and Task budget increases:

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true
```

The requester creates an exact ActionIntent at `POST /api/v1/policy/actions`. An `APPROVER` reviews
the pending item through `/api/v1/approvals` and approves or rejects it. Approval returns a
short-lived `permit_id`; the original requester supplies it exactly once:

```bash
curl -X POST http://localhost:8000/api/v1/agent-versions/<version-id>/publish \
  -H "Authorization: Bearer <publisher-token>" \
  -H "Execution-Permit-Id: <permit-id>" \
  -H "Content-Type: application/json" \
  -d '{"verified_capabilities":["document.summarize"],"make_default":true}'
```

The Permit is bound to the requester, tenant, action, resource and canonical arguments. See the
[Policy/Approval baseline](docs/architecture/modules/policy-approval-baseline-implementation.md).

### Enable the governed MCP Registry

The governed Registry is also explicit opt-in because it requires authenticated providers and the
Policy boundary:

```dotenv
AGENTMESH_FEATURE_GATES=mcp_read_tools=true,identity_rbac=true,policy_approval=true,governed_mcp=true
```

`TOOL_PROVIDER` callers manage MCP Servers and immutable Tool snapshots under `/api/v1/mcp`.
Read-only Versions can publish directly; Versions containing any write-class Tool require an exact
Policy approval and one-time Permit. Runtime Catalog resolution accepts only active, published,
unambiguous bindings and rejects live MCP Schema drift. Published read-only Streamable HTTP
Servers use clean HTTPS endpoints, public-address DNS pinning, verified TLS, no redirects/proxies,
bounded responses, and a fresh MCP session for every invocation. Configure
`AGENTMESH_MCP_HTTP_TIMEOUT_SECONDS` to 1-300 seconds. See the
[Governed MCP Registry baseline](docs/architecture/modules/governed-mcp-registry-implementation.md)
and [Streamable HTTP runtime](docs/architecture/modules/mcp-streamable-http-implementation.md).

Tool Providers can explicitly refresh a public published Server Version through
`POST /api/v1/mcp/server-versions/<version-id>/discovery-snapshots`. Compatible and expanded
snapshots preserve existing bindings without exposing new Tools; failed, incompatible, or expired
snapshots block Catalog resolution. Configure `AGENTMESH_MCP_DISCOVERY_TTL_SECONDS` and
`AGENTMESH_MCP_DISCOVERY_MAX_TOOLS`. See the
[capability refresh baseline](docs/architecture/modules/mcp-capability-refresh-implementation.md).

Idempotent MCP writes are a separate explicit opt-in:

```dotenv
AGENTMESH_FEATURE_GATES=mcp_read_tools=true,identity_rbac=true,policy_approval=true,governed_mcp=true,mcp_write_tools=true
```

Only published Streamable HTTP Tools classified as `IDEMPOTENT_WRITE` are executable. Their input
schema must require a string `idempotency_key`. Request an exact approval through
`POST /api/v1/mcp/tool-execution-intents`, approve it independently, then create the Task with its
one-time `Execution-Permit-Id`. AgentMesh persists a task-bound authorization before execution,
retries an uncertain delivery at most once with the same arguments/key, and records
`OUTCOME_UNKNOWN` when no result can be confirmed. `NON_IDEMPOTENT_WRITE` and `IRREVERSIBLE` remain
disabled. See the [safe write runtime](docs/architecture/modules/mcp-safe-write-implementation.md).

### Enable the trusted A2A Peer Registry

A2A federation trust is explicit opt-in and requires authenticated operators:

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true,a2a_federation=true
```

`FEDERATION_OPERATOR` callers can register tenant-scoped Peers and import immutable A2A v1 Agent
Card snapshots under `/api/v1/a2a`. They can also fetch the registered standard well-known URL with
`POST /api/v1/a2a/peers/{peer_id}/agent-cards:discover`. Discovery uses public-address-pinned HTTPS,
bounded JSON, no redirects, ETag and bounded Cache-Control TTLs. A discovered snapshot remains a
candidate until an operator calls
`POST /api/v1/a2a/peers/{peer_id}/agent-cards/{snapshot_id}:activate`; discovery never expands trust
automatically. Endpoint host/binding allowlists, expiry-aware resolution, idempotency and audit are
enforced. Skills remain declared candidates rather than verified capabilities. See the
[A2A Peer Registry baseline](docs/architecture/modules/a2a-peer-registry-implementation.md) and
[controlled Agent Card discovery](docs/architecture/modules/a2a-agent-card-discovery-implementation.md).

To create `FEDERATED` Tasks and send them to an A2A 1.0 HTTP+JSON Peer, enable the
separate governed delegation Gate:

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true,a2a_federation=true,a2a_delegation=true
```

Delegation requires an exact Policy approval and one-time Permit. The send is persisted before
network I/O and is never automatically repeated when delivery is uncertain; operators inspect and
explicitly poll durable correlations under `/api/v1/a2a/delegations`. Public Peers need no further
configuration.

Automatic polling is a separate opt-in process. Add `a2a_reconciliation=true` to the Gate list and
run `agentmesh-a2a-reconciler`, or start the Compose profile with
`docker compose --profile a2a up`. Reconcilers claim due rows with PostgreSQL `SKIP LOCKED`, use
short crash-recoverable leases and bounded exponential failure backoff, and stop at terminal or
intervention states. Initial sends with unknown delivery and no remote Task ID are never guessed or
retried. Operators can issue an idempotent best-effort remote cancel through
`POST /api/v1/a2a/delegations/{correlation_id}/cancel`; only a confirmed remote canceled state
cancels the local Task, while races preserve the actual completion or failure. Streaming and push
callbacks remain deferred. See the
[outbound A2A delegation baseline](docs/architecture/modules/a2a-outbound-delegation-implementation.md)
and [automatic reconciliation](docs/architecture/modules/a2a-reconciliation-implementation.md),
plus [controlled remote cancellation](docs/architecture/modules/a2a-remote-cancellation-implementation.md).

Unknown MCP write and initial A2A send outcomes can be closed only from independently collected
operator evidence. Enable the explicit reconciliation Gate (and the relevant MCP/A2A Gates):

```dotenv
# MCP writes
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true,human_resolution=true,mcp_read_tools=true,governed_mcp=true,mcp_write_tools=true,outcome_reconciliation=true

# A2A sends
AGENTMESH_FEATURE_GATES=identity_rbac=true,policy_approval=true,human_resolution=true,a2a_federation=true,a2a_delegation=true,outcome_reconciliation=true
```

MCP commands confirm success/failure without replaying the Tool. A2A commands either bind a known
remote Task ID for normal polling or confirm non-delivery without repeating Send Message. Both
require an evidence reference, SHA-256 evidence digest, reason and `Idempotency-Key`. See
[operator outcome reconciliation](docs/architecture/modules/operator-outcome-reconciliation-implementation.md).

For a Peer whose active Agent Card declares one HTTP Bearer security requirement, enable the
metadata-only Credential Broker as well:

```dotenv
AGENTMESH_FEATURE_GATES=identity_rbac=true,persistent_identity=true,policy_approval=true,a2a_federation=true,a2a_delegation=true,credential_broker=true
AGENTMESH_CREDENTIAL_WORKLOAD_PRINCIPAL_ID=<active-service-principal-uuid>
AGENTMESH_CREDENTIAL_LEASE_TTL_SECONDS=60
```

Create a SecretReference under `/api/v1/credentials` using the name of an environment variable,
then approve and create an exact workload/Peer/Card/audience/scope binding. Put the actual value
only in the API process environment; do not send it through the API or store it in Agent state.
Each A2A send or poll resolves a fresh short-lived lease and injects the Bearer header inside the
HTTPS adapter. User bearer passthrough, Basic/API-key schemes, OAuth exchange and mTLS are rejected
by this baseline. See the [Workload Credential Broker baseline](docs/architecture/modules/workload-credential-broker-implementation.md).

The same Broker can authorize a published read-only MCP Streamable HTTP Server without enabling
A2A. Enable `identity_rbac`, `persistent_identity`, `policy_approval`, `mcp_read_tools`,
`governed_mcp`, and `credential_broker`; then create an `MCP_HTTP_BEARER` SecretReference and an
exact MCP binding through `/api/v1/credentials`. Authentication-required Servers never downgrade
to anonymous execution. Secret values remain process-environment inputs only.

### Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d postgres redis
alembic upgrade head
agentmesh-seed
uvicorn agentmesh.api.app:app --reload
```

Run the relay and worker in two additional terminals:

```bash
agentmesh-relay
agentmesh-worker
```

On PowerShell, activate the virtual environment with `.venv\Scripts\Activate.ps1`.

The local defaults use `127.0.0.1` explicitly so PostgreSQL connections behave consistently across Windows, WSL, and Docker Desktop. Container-to-container connections continue to use the Compose service name `postgres`.

Run the fast test suite with:

```bash
ruff check .
pytest
```

With PostgreSQL and Redis running and migrations applied, include the real transport,
persistence, and checkpoint test with:

```bash
AGENTMESH_RUN_POSTGRES_TESTS=1 pytest -m postgres
```

On PowerShell, set the flag with `$env:AGENTMESH_RUN_POSTGRES_TESTS="1"`.

Install the optional Langfuse adapter with `pip install -e ".[dev,observability]"` before enabling `AGENTMESH_LANGFUSE_ENABLED`.

## Design principles

1. Single-agent by default; multi-agent by demonstrated need.
2. PostgreSQL is the business source of truth.
3. Agent conversation is not a substitute for a workflow state machine.
4. Every handoff carries a typed contract and explicit acceptance criteria.
5. High-risk actions require least privilege and policy-controlled approval.
6. Durable state and idempotency take precedence over clever prompting.
7. Observability is part of the execution contract, not an afterthought.
8. Protocols are boundaries: A2A for agent delegation, MCP for tools and context.

## Current scope

The Alpha implements the accepted single-team v1 scope: direct, reviewed, and coordinated Subtask
DAG execution; durable PostgreSQL/Redis delivery; fenced Attempts and LangGraph checkpoints;
versioned Agent/MCP/A2A registries; governed MCP reads and idempotent writes; controlled A2A
delegation, polling, cancellation, and reconciliation; Policy approvals; opt-in Identity/RBAC;
content-addressed Artifacts; budgets and quotas; Langfuse export; and the replayable Mission Map
Console.

The `minimal` profile keeps advanced governance and federation disabled. Cross-tenant RLS and fair
dispatch, managed HA/PITR, cloud secret and object-store adapters, A2A streaming/push and remote
Artifact transfer remain post-v1 extensions. See [v1 completion scope](docs/v1-completion-scope.md)
for the exact support boundary.

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing architecture or implementation
changes.

## License

Licensed under the [Apache License 2.0](LICENSE).
