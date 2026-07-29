# Virtual Company operating model

Status: Proposed

## Outcome

AgentMesh should evolve from a governed multi-Agent execution platform into a **Virtual Company
Operating System**. The user acts as owner and chief executive. Long-lived digital employees occupy
defined positions, work through departments, pursue company goals, use approved business systems,
retain governed experience, and produce measurable business outcomes.

The company layer does not replace the existing Task runtime. It translates strategy and recurring
operations into authorized Tasks, then derives company state from durable Task, Artifact, Policy,
Tool, A2A, usage, and financial evidence.

The intended experience is:

1. The owner describes a company mission, market, constraints, and risk appetite.
2. A Chief of Staff proposes goals, departments, positions, operating rhythms, and budgets.
3. The owner approves the organizational plan and appoints Agent Versions to positions.
4. Departments create bounded work from approved goals and recurring operations.
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

- Company, Department, Position, and Appointment;
- Company Goal, Objective, Key Result, Initiative, and Operating Cycle;
- recurring Operation and event Trigger;
- business Objects and relationships;
- company, department, employee, relationship, and episodic Memory;
- Business Metric, Budget Allocation, Revenue Evidence, and Management Review;
- reusable Company Templates.

The company layer must call the execution plane through public application services. It must not
write Task or Run tables directly.

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

### Department

```text
Department
- id
- company_id
- key
- name
- purpose
- parent_department_id
- budget_policy_id
- memory_namespace
- status
```

Initial departments may include Product, Research, Sales, Marketing, Delivery, Customer Success,
Finance, Risk, and Operations. Departments are configurable; they are not hard-coded enum values.

### Position

```text
Position
- id
- department_id
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

Recommended built-in positions:

| Position | Responsibility |
|---|---|
| Owner | Mission, risk appetite, capital, final high-impact approval |
| Chief of Staff | Convert owner intent into cycles, objectives, initiatives, and reviews |
| COO | Capacity, recurring operations, dependencies, and delivery exceptions |
| Department Head | Department planning, quality, budget proposals, and escalations |
| Specialist | Execute bounded work using assigned capabilities |
| Finance Controller | Cost/revenue evidence, budget controls, and payment proposals |
| Risk Officer | Policy exceptions, external action review, and compliance evidence |

These are templates. A small company may bind several positions to one Agent Definition, but every
decision remains attributable to its Position and Run.

## Office projection

The Office becomes a spatial view of the organization:

- departments correspond to durable Department records;
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

- `company_model`: Company, Department, Position, and Appointment.
- `company_goals`: Operating Cycle, Objective, Key Result, and Initiative.
- `company_operations`: recurring Operations and Triggers.
- `organizational_memory`: governed long-term Memory.
- `business_objects`: typed business object Registry.
- `financial_governance`: revenue, expense, budget, and payment controls.
- `company_templates`: reusable company configurations.

All are off by default. The existing v1 profiles remain unchanged.

## Delivery slices

### Slice 1 — organization and manual cycle

- create one Company;
- configure Departments and Positions;
- appoint published Agent Versions;
- create one Operating Cycle, Objective, Key Result, and Initiative;
- explicitly launch Tasks from an Initiative;
- project organization and goals in Console and Office;
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

- package departments, positions, goals, operations, policies, and qualification fixtures;
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
- Deterministic CI demonstrates a complete operating cycle without paid APIs or network access.

## Non-goals

- forming or representing a legal company;
- granting Agents legal personhood or signing authority;
- autonomous investment or trading;
- unrestricted spending, outreach, hiring, or contract execution;
- replacing accounting, CRM, banking, or payroll systems of record;
- cross-tenant scheduling;
- fictional employee emotions, levels, or off-ledger conversations.

## Related detailed proposals

- [Organizational memory service](organizational-memory-service.md)
- [Company operations and business objects](company-operations-and-business-objects.md)
- [Revenue and financial governance](revenue-and-financial-governance.md)
- [Market intelligence studio template](market-intelligence-studio-template.md)

Each detailed proposal may be implemented independently behind its own Feature Gate. The
organizational model is the common identity and authorization foundation.
