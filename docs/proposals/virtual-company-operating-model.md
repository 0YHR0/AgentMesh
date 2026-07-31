# Virtual Company operating model

Status: Partially implemented (Company primitives, governed economics, and declarative Packs)

## Outcome

AgentMesh should evolve from a governed multi-Agent execution platform into a **Virtual Company
Operating System**. The user may operate anything from a small Agent team to a structured virtual
company. Long-lived digital employees occupy defined positions, participate in organization units
and project teams, pursue approved goals, use governed business systems, retain governed
experience, and produce measurable business outcomes.

The company layer does not replace the existing Task runtime. It translates strategy and recurring
operations into authorized Tasks, then derives company state from durable Task, Artifact, Policy,
Tool, A2A, usage, and financial evidence.

The intended experience is:

1. The owner describes a company mission, market, constraints, and risk appetite.
2. A planning Agent or Chief of Staff proposes goals, organization units, positions, operating
   rhythms, and budgets.
3. The owner approves the organizational plan and appoints Agent Versions to positions.
4. Organization units and project teams create bounded work from approved goals and recurring
   operations.
5. Digital employees collaborate through explicit dependencies and Handoffs.
6. External actions pass through Tool, Policy, credential, and financial controls.
7. Results update KPIs and become candidate organizational memories.
8. The company reviews evidence and adjusts its next operating cycle.

“Virtual employee” means durable identity, role, authority, history, and memory continuity. It does
not imply consciousness, legal personhood, or permission to act outside the owner’s delegated
authority.

## Product boundary

AgentMesh already owns execution-plane concepts:

- Agent Definition and immutable Agent Version;
- Task, Subtask, Run, Attempt, Handoff, Artifact, and checkpoint;
- Goal Contract, Plan Patch, budget, quota, approval, and resolution;
- MCP Tool and A2A Peer boundaries;
- audit, interaction projection, Console, and Office.

The proposed company layer adds:

- Company, Organization Unit, Position, Appointment, and organizational relationships;
- Company Goal, Objective, Key Result, Initiative, and Operating Cycle;
- recurring Operation and event Trigger;
- business Objects and relationships;
- company, department, employee, relationship, and episodic Memory;
- Business Metric, Budget Allocation, Revenue Evidence, and Management Review;
- reusable Company Templates.

The company layer must call the execution plane through public application services. It must not
write Task or Run tables directly.

The scope is digital work whose inputs can be read through authorized interfaces, whose actions can
be mediated by Tools or A2A peers, and whose outcomes can be verified with evidence. Physical or
legally reserved work requires external systems and accountable human operators; calling the
platform an operating system does not remove that boundary.

## Control-plane model

AgentMesh is analogous to Kubernetes at the control-plane level: it reconciles declared intent with
observed execution state through durable, policy-governed resources. It is not a container
orchestrator and does not copy the Kubernetes API. The analogy defines a layering rule:

| Layer | Responsibility | Examples |
|---|---|---|
| L0 — execution runtime | Govern and observe Agent workloads | Task, Run, Agent Version, Handoff, Artifact, MCP, A2A, Policy |
| L1 — company primitives | Represent durable organizational intent | Company, Organization Unit, Position, Appointment, Goal, Operation, Memory |
| L2 — Domain Packs | Add bounded business semantics without changing the core | object schemas, workflows, policies, connectors, metrics, Office assets |
| L3 — Company Templates | Compose versioned Packs into an installable starting point | software studio, market-intelligence studio, custom user template |

The runtime must not contain hard-coded industries, department names, job titles, CRM fields, or
accounting workflows. A Domain Pack registers these definitions through stable extension contracts.
A Company Template selects and configures Packs; it is not a new runtime.

### Pack contract

A versioned Company Template may compose:

- an `OrganizationPack` for units, positions, relationships, and default appointments;
- an `AgentPack` for Agent Definitions, qualification Tasks, and capability requirements;
- a `WorkflowPack` for Operations, Triggers, Goal templates, and acceptance contracts;
- a `BusinessObjectPack` for schemas, lifecycles, actions, and projections;
- a `MemoryPack` for namespaces, retrieval policies, and candidate-learning rules;
- a `ConnectorPack` for MCP servers, A2A peers, credential requirements, and health checks;
- a `PolicyPack` for budgets, approvals, risk tiers, and data boundaries;
- an `OfficePack` for optional spatial presentation and semantic assets.

Every Pack has an identifier, semantic version, content digest, dependencies, compatible runtime
range, required Feature Gates, migration plan, and uninstall policy. Installation must show a
preview of resources, permissions, Tools, credentials, and approval surfaces before applying it.
The first implementation accepts declarative resources only; arbitrary Pack-supplied executable
code is outside the trust boundary. Upgrades are explicit, versioned, auditable, and reversible
when the Pack declares a safe downgrade.

### Progressive adoption

Users choose how much company structure they need:

1. **Agent Team** — use the existing Task runtime, Registry, Handoffs, and governance without any
   Company resources.
2. **Company Template** — install a reviewed template, enable only its required Packs, and customize
   names, Agents, budgets, connectors, and policies.
3. **Custom Company** — compose user-defined units, positions, relationships, objects, workflows,
   and Packs without inheriting the classic-company layout.

Feature Gates apply at the capability level, while Pack installation applies at the business-model
level. Disabling or uninstalling an optional Pack must not make the L0 runtime unusable.

## Design principles

### Evidence before narrative

Company dashboards derive progress, revenue, cost, and employee performance from persisted
evidence. Agent-generated summaries are annotations, not financial or operational truth.

### Bounded autonomy

An employee may autonomously create or execute work only within an approved Operation, budget,
Tool allowlist, data scope, and risk tier. Exceeding any bound creates a proposal or approval gate.

### Position is not identity

An Agent Definition represents a digital employee identity. A Position represents a company
responsibility. An Appointment binds an immutable Agent Version to a Position for a time interval.
Changing an appointment does not rewrite historical ownership.

### Memory is governed state

Long-term memory is proposed, reviewed, versioned, attributable, expirable, and access-controlled.
It is not an unbounded dump of model conversations.

### Business objects over chat

Customers, opportunities, reports, campaigns, invoices, and payment requests are typed objects.
Agents can discuss them, but durable operations target object identities and versions.

### Humans retain legal and financial authority

The initial platform may prepare commercial actions and execute explicitly delegated low-risk
operations. Contracts, material spending, payments, regulated filings, employment decisions, and
other high-impact actions remain approval-gated.

## Organizational model

### Company

```text
Company
- id
- name
- mission
- owner_principal_id
- status
- risk_policy_id
- default_currency
- operating_timezone
- current_cycle_id
- created_at
```

The first implementation remains single-company and single-team at runtime, while retaining stable
identifiers for future expansion. A Company is not a security tenant in this proposal.

### Organization unit

```text
OrganizationUnit
- id
- company_id
- key
- name
- kind
- purpose
- parent_unit_id
- budget_policy_id
- memory_namespace
- status
```

`kind` is registered rather than enumerated by the core. A Department is the built-in presentation
for a conventional functional unit, while a project, studio, guild, portfolio, regional team, or
user-defined unit may use the same primitive. Initial templates may include Product, Research,
Sales, Marketing, Delivery, Customer Success, Finance, Risk, and Operations; none are required.

Organization is a graph rather than a fixed management tree. Versioned edges may describe
membership, reporting, service ownership, project participation, or matrix relationships. The
`parent_unit_id` field supplies an optional navigation hierarchy, not the complete authorization
model. Cycles and Tasks bind to stable unit and Position identities, so reorganizations do not
rewrite historical responsibility.

### Position

```text
Position
- id
- primary_unit_id
- key
- title
- responsibility_contract
- required_capabilities
- allowed_tool_capabilities
- memory_policy_id
- approval_scope
- budget_scope
- reports_to_position_id
- status
```

The responsibility contract defines outcomes, decision rights, prohibited actions, escalation
rules, and expected evidence.

### Appointment

```text
Appointment
- id
- position_id
- agent_definition_id
- agent_version_id
- starts_at
- ends_at
- appointed_by
- reason
- status
```

Only a published Agent Version may receive an active appointment. Appointment changes are audited
and do not mutate existing Runs.

## Employee lifecycle

The lifecycle is organizational rather than game-like:

```text
Candidate
  → Onboarding
  → Active
  → Restricted
  → Suspended
  → Offboarded
```

Onboarding verifies capabilities, Tool access, Memory access, Policy scope, budget, and a
deterministic qualification task. Restriction or suspension prevents new assignments but preserves
history.

Meaningful employee development is represented by:

- new immutable Agent Versions;
- verified capability certifications;
- reduced or expanded approval requirements;
- additional Tool grants;
- larger or smaller budget scopes;
- appointment to a different Position;
- reviewed procedural memories and feedback.

There are no fictional experience points. Performance metrics cannot automatically grant authority
without an approved appointment or Policy change.

## Strategy and operating hierarchy

```text
Company Mission
  └─ Operating Cycle
      ├─ Objective
      │   ├─ Key Result
      │   └─ Initiative
      │       └─ Task / Coordinated Task
      └─ Recurring Operation
          └─ Triggered Task
```

The owner approves the Company Mission and each Operating Cycle. Objectives define desired
business outcomes. Key Results define measurable evidence. Initiatives organize bounded work.
Tasks remain the unit of execution.

AgentMesh must distinguish:

- proposed goals from approved goals;
- measured values from Agent estimates;
- activity counts from business outcomes;
- revenue evidence from cash receipt;
- cost reservation from settled cost.

## Company control loop

The default operating loop is:

```text
Observe
  → Diagnose
  → Propose
  → Approve
  → Execute
  → Verify
  → Measure
  → Learn
  → Adjust
```

Each transition produces evidence:

- Observe: imported metric or external event.
- Diagnose: analysis Artifact with sources.
- Propose: Goal, Initiative, Operation, or governed action candidate.
- Approve: Policy and human decision.
- Execute: Task/Run/Tool evidence.
- Verify: review, acceptance criteria, or reconciliation.
- Measure: KPI or financial evidence update.
- Learn: candidate Memory records.
- Adjust: next-cycle decision or Plan Patch.

The loop may pause indefinitely at approval or missing evidence without manufacturing progress.

## Management roles

Recommended positions in the classic-company reference template:

| Position | Responsibility |
|---|---|
| Owner | Mission, risk appetite, capital, final high-impact approval |
| Chief of Staff | Convert owner intent into cycles, objectives, initiatives, and reviews |
| COO | Capacity, recurring operations, dependencies, and delivery exceptions |
| Department Head | Department planning, quality, budget proposals, and escalations |
| Specialist | Execute bounded work using assigned capabilities |
| Finance Controller | Cost/revenue evidence, budget controls, and payment proposals |
| Risk Officer | Policy exceptions, external action review, and compliance evidence |

These roles are template data, not required runtime types. A small company may omit them or bind
several positions to one Agent Definition, but every decision remains attributable to its Position
and Run. Other templates may use entirely different organization and authority graphs.

## Office projection

The Office becomes a spatial view of the organization:

- rooms and zones may correspond to durable Organization Unit records;
- employees correspond to active Appointments;
- a desk location remains a presentation preference, not proof of employment or activity;
- Objective and Initiative state appears on department boards;
- recurring Operations appear as scheduled work queues;
- Handoffs, MCP, A2A, and approval packets remain event-backed;
- financial and customer data is redacted by permission;
- employee memory and performance details are accessible through an authorized profile, not shown
  as fictional levels.

The Admin Console remains the precise control and audit surface.

## Feature gates

Recommended gates:

- `company_model`: Company, Organization Unit, Position, Appointment, and relationship graph.
- `company_goals`: Operating Cycle, Objective, Key Result, and Initiative.
- `company_operations`: recurring Operations and Triggers.
- `organizational_memory`: governed long-term Memory.
- `business_objects`: typed business object Registry.
- `financial_governance`: revenue, expense, budget, and payment controls.
- `company_templates`: reusable company configurations.
- `company_packs`: declarative Pack registry, install planning, lifecycle, and dependency checks.

All are off by default. The existing v1 profiles remain unchanged.

## Delivery slices

### Slice 1 — organization and manual cycle

- [x] create one active Company per tenant;
- [x] configure generic Organization Units, Positions, and relationship graph;
- [x] appoint capability-qualified published Agent Versions with auditable history;
- [x] expose feature-gated REST API and project active Appointments into the Office;
- [x] create versioned Operating Cycles, Objectives, Key Results, and Initiatives;
- [x] explicitly launch direct Tasks from an approved, active Initiative;
- [x] keep verified and estimated Key Result measurements separate;
- [x] expose nested goal/Task evidence and project active counts into the Office;
- retain deterministic fixtures without external providers.

### Slice 2 — recurring operations

- define bounded Operations and schedules;
- create Tasks from due schedules or authorized events;
- enforce per-Operation budget, concurrency, Tool, and approval limits;
- add pause, disable, replay, and missed-schedule handling.

### Slice 3 — memory and business objects

- introduce governed organizational Memory;
- register typed customer, opportunity, deliverable, and financial objects;
- assemble role-specific context through permission-aware retrieval;
- produce candidate learning after reviewed work.

### Slice 4 — company templates

- implement the declarative Pack registry, dependency validation, installation preview, and
  lifecycle;
- package units, positions, goals, operations, policies, and qualification fixtures;
- instantiate the market-intelligence studio template;
- support import/export with versioned digests;
- add a guided owner setup flow.

## Acceptance criteria

- Every visible employee has an active, auditable Appointment to a Position.
- A Company Objective can be traced to Initiatives, Tasks, evidence, and measured Key Results.
- No scheduled Operation exceeds its approved budget, Tool, data, or concurrency boundary.
- Restarting API/Worker does not lose organization, schedule, or execution state.
- Memory cannot be written or retrieved outside the Position’s Memory Policy.
- Company revenue and performance dashboards distinguish verified, estimated, and proposed values.
- High-impact external or financial actions remain approval-gated.
- Disabling all company gates restores the existing single-team v1 behavior.
- A minimal Agent Team runs without creating a Company or conventional Department.
- The same core can instantiate two structurally different templates without hard-coded role,
  department, business-object, or industry names.
- Disabling or uninstalling an optional Pack preserves L0 execution and retained audit evidence.
- Pack installation previews dependencies, permissions, credential needs, migrations, and governed
  side effects before mutating company state.
- Deterministic CI demonstrates a complete operating cycle without paid APIs or network access.

## Non-goals

- forming or representing a legal company;
- granting Agents legal personhood or signing authority;
- autonomous investment or trading;
- unrestricted spending, outreach, hiring, or contract execution;
- replacing accounting, CRM, banking, or payroll systems of record;
- claiming that non-digital or legally reserved work can run without external adapters and
  accountable operators;
- cross-tenant scheduling;
- fictional employee emotions, levels, or off-ledger conversations.

## Related detailed proposals

- [Employee-first virtual company and extension platform](employee-first-virtual-company-and-extension-platform.md)
- [Organizational memory service](organizational-memory-service.md)
- [Company operations and business objects](company-operations-and-business-objects.md)
- [Revenue and financial governance](revenue-and-financial-governance.md)
- [Market intelligence studio template](market-intelligence-studio-template.md)

Each detailed proposal may be implemented independently behind its own Feature Gate. The
organizational model is the common identity and authorization foundation.
