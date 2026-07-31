# Employee-first virtual company and extension platform

Status: Proposed

## Decision summary

AgentMesh will present itself to ordinary users as a system for creating, developing, and managing
durable AI employees inside a virtual company. The reliable Agent control plane remains the
implementation foundation, but its technical resources are progressively hidden behind five
user-facing concepts:

1. Company;
2. Employee;
3. Goal;
4. Approval;
5. Result.

The core repository will become a business-neutral Agent workload control plane. Industry
semantics belong in versioned Packs and, after the extension contracts stabilize, independently
deployed Controllers. The Market Intelligence Studio remains the first in-repository reference
implementation until it can run through public extension contracts without importing core
internals or writing the AgentMesh database.

Employee development is evidence-backed operational improvement. It is not fictional experience,
emotion, or an automatically increasing level. A development outcome may propose reviewed Memory,
a capability certification, a new immutable Agent Version, a Tool grant, a model-policy change,
or a new Appointment. Authority never increases merely because a model claims that it learned.

## Product promise

The intended owner experience is:

> Build a company, recruit and develop employees, give the company goals, observe real
> collaboration, approve exceptional actions, and receive durable results.

The owner should not need to understand Task Runs, Agent Version digests, MCP Server Versions,
Credential Bindings, DAG concurrency, or Feature Gates before completing the first useful goal.
Those resources remain visible in Advanced mode for operators and developers.

The first-value path must be possible in less than ten minutes on a supported local installation:

```text
Choose a Company Template
  -> choose Demo or connect one model provider
  -> connect only the Tools required by the Template
  -> create the Company and its starter employees
  -> enter one Goal
  -> watch evidence-backed collaboration
  -> inspect the Result and any required Approval
```

Demo mode requires no paid provider or network Tool. Standard mode should normally require one
provider credential plus scenario-specific connector authorization. Advanced mode exposes the
complete control plane.

## Product personas and surfaces

### Company owner

The owner manages Company, Employee, Goal, Approval, and Result. The Office is the primary daily
surface. The owner may open the Admin Console for precise configuration and audit, but normal work
does not require it.

### Pack developer

The Pack developer declares a business model, role requirements, resource schemas, workflows,
Policies, Tool dependencies, Memory Policies, qualification fixtures, and optional presentation
assets. Most Packs should not require executable Controller code.

### Controller developer

The Controller developer implements a bounded reconciliation loop when declarative workflow and
projection rules cannot express the domain. A Controller uses public APIs and stable SDK
contracts. It never imports internal repositories or accesses PostgreSQL directly.

### Platform operator

The operator manages identity, provider policies, credentials, MCP/A2A trust, quotas, feature
availability, health, retention, backup, and audit. Existing low-level control-plane views remain
available to this persona.

## Employee domain model

The initial implementation does not introduce a second identity competing with Agent Definition.
It establishes this stable mapping:

| Product concept | Authoritative AgentMesh resource |
|---|---|
| Employee identity | Agent Definition |
| Current working method | published default Agent Version |
| Company responsibility | Position |
| Current employment assignment | active Appointment |
| Personal reviewed experience | Employee Memory namespace |
| Authorized systems | Agent Version Tool profile plus Credential/Policy boundary |
| Work history | Task, Run, Artifact, review, usage, and Appointment evidence |
| Office character | presentation profile keyed by Agent Definition |

An `EmployeeProfile` is initially a read projection across these resources, not a new source of
truth. This avoids duplicating lifecycle and authorization state.

Recommended projection:

```text
EmployeeProfile
- employee_id                 # Agent Definition ID
- display_name
- description
- presentation_profile       # avatar/preset, locale, restrained work style
- lifecycle                  # derived from Definition and active Appointment
- current_position
- current_appointment
- current_agent_version
- verified_capabilities
- tool_access_summary
- memory_policy_summary
- current_assignment_summary
- evidence_backed_metrics
- appointment_history
- development_recommendations
```

The presentation profile may describe communication preferences and a visual character. It must
not assert consciousness, emotion, protected personal attributes, or unsupported psychological
judgments.

## Employee creation

The ordinary creation form asks only for:

- name;
- desired job or selected role blueprint;
- optional communication/work style;
- Company/department placement;
- Company-default or explicitly selected model policy.

The application service expands that request into a governed transaction or resumable setup
operation:

1. create an Agent Definition;
2. create a draft Agent Version from the selected Role Blueprint;
3. inherit the Company provider policy without copying a raw API key;
4. bind declared capabilities and the minimum Tool allowlist;
5. run Template qualification checks;
6. publish the qualified Version;
7. create an Appointment to the selected Position;
8. create the Employee Memory namespace/policy binding;
9. create the Office presentation profile and placement.

If a required provider, Tool, credential, Position, or Policy is unavailable, creation stops with a
small list of actionable Conditions. It must not leave an apparently active but unusable employee.

The advanced Registry workflow remains available for custom Agent Versions and externally managed
Agents.

An Employee runtime is replaceable. A Role Blueprint declares capabilities and contracts rather
than requiring one framework. A compatible Employee may be backed by the built-in model loop, a
deterministic adapter, LangGraph, an OpenAI Agents SDK adapter, a coding-agent adapter, or a trusted
A2A peer. The Employee identity, Appointment, Memory, work history, and Office character remain
stable when the runtime changes. Provider credentials are configured at Company/operator scope and
leased at execution time; ordinary setup does not copy an API key into every Employee.

## Meaningful employee development

### Development inputs

Development recommendations may derive from:

- accepted and rejected Results;
- independent Reviewer feedback;
- acceptance criteria and evaluation fixtures;
- verified cost, latency, retry, and deadline evidence;
- Tool-use evidence and governed outcome reconciliation;
- reviewed Memory candidates;
- qualification Tasks;
- recurring failure or collaboration patterns supported by multiple Runs.

Model self-assessment is never sufficient evidence.

### Development plan

```text
EmployeeDevelopmentPlan
- id
- company_id
- employee_id
- target_position_id          # optional
- desired_capabilities
- evidence_requirements
- training_task_templates
- evaluator_requirement
- budget
- status
- proposed_by
- approved_by
- created_at
- completed_at
```

Recommended lifecycle:

```text
DRAFT -> APPROVED -> TRAINING -> EVALUATING -> CHANGE_PROPOSED -> COMPLETED
                    |              |                    |
                    +-----------> FAILED <--------------+
```

The completed plan records one or more governed changes:

- accepted procedural or episodic Memory;
- capability certification evidence;
- a proposed immutable Agent Version;
- a Tool-access proposal;
- a model-policy proposal;
- an Appointment or promotion proposal;
- no change, when the evaluation fails.

Mentoring is a bounded Development Plan relationship, not an invented social bond. A mentor may
review work, supply an approved procedure, or participate in an evaluation Task. Memory is not
copied wholesale between employees, and mentor status does not grant access or authority outside
the mentor's own Position and Policy.

### Agent Version improvement

AgentMesh never mutates the currently published Version in place. A recommendation produces a
diffable draft Version that identifies:

- source Version;
- evidence and evaluation set;
- instruction changes;
- model-policy changes;
- Tool-profile changes;
- declared/verified capability changes;
- expected benefit and regression risks.

Publishing and making it the default remain explicit governed actions. Existing Runs retain their
original Version digest.

### Performance and growth

The employee profile may show:

- verified task categories completed;
- acceptance and independent review rate;
- revision and escalation rate;
- evidence completeness;
- deadline reliability;
- actual Token/cost usage;
- capabilities with certification evidence;
- common failure categories;
- development plans and their outcomes.

AgentMesh does not assign fictional XP, morale, loyalty, or an unexplained aggregate level. A
visual skill tree is permitted only when every node maps to a capability and its evidence.

Metrics must include scope and sample size. They cannot automatically grant authority, Tool
access, a larger budget, or a promotion.

## Goal-first collaboration

Owners create a business Goal or domain resource rather than a low-level Task DAG. A minimal
request includes:

```text
GoalRequest
- objective
- expected_result_type
- deadline                  # optional
- budget                    # optional
- required_employee_ids     # optional
- excluded_employee_ids     # optional
- preferred_department_ids  # optional
- autonomy_level
```

A domain Controller or declarative Operation turns this into a Goal Contract and one or more
Tasks. Direct Task creation remains an Advanced feature and public control-plane API.

### Team composition

The `TeamComposer` recommends employees using only persisted facts:

- active Appointment and Position responsibility;
- immutable verified capabilities;
- Tool and credential readiness;
- model/runtime availability;
- current assignments and capacity;
- approved budget and quota;
- evidence-backed quality/cost/latency history;
- required independence or separation of duties;
- explicitly selected/excluded employees;
- scenario workflow requirements.

The first implementation is deterministic and explainable. It returns a proposal with the reason
for every selected employee and every blocker. LLM advice may rank already-qualified candidates,
but it cannot override hard eligibility, security, budget, or separation-of-duties constraints.

Owners may choose:

- automatic team selection;
- select a responsible department;
- require a particular employee;
- inspect and confirm the proposed team;
- manually compose a team in Advanced mode.

### Collaboration evidence

Visual and narrative collaboration derives from Task, Subtask, Run, Handoff, Tool Invocation,
A2A, Approval, Artifact, and Controller events. There is no hidden employee conversation that
exists only to make the Office appear alive.

## Office product experience

The Office becomes the owner-facing Company surface:

- create a Goal from the Company desk;
- click an employee to inspect responsibilities, current work, evidence-backed capabilities,
  Memory review queue, and development plans;
- inspect the proposed team before a high-cost or high-autonomy Goal;
- observe persisted work ownership, Handoffs, meetings, Tool use, and Approval waiting;
- open Result Artifacts and Business Objects from the producing employee/team;
- initiate training, mentoring, reassignment, restriction, or offboarding;
- drag an employee within a room to change presentation placement;
- drag an employee to another department to open a transfer proposal, never silently rewrite the
  Appointment.

Ambient walking, desk animation, and social staging are marked as ambient presentation. They do
not create Runs, consume model budget, claim learning, or imply private reasoning.

The Admin Console remains the authoritative operator and audit surface. Every simplified Office
action links to its underlying resources and evidence.

## Progressive disclosure

### Demo mode

- zero provider credentials;
- deterministic employees and Tools;
- one installed reference Company;
- one complete Goal -> collaboration -> Approval -> Result fixture;
- no external writes.

### Standard mode

- Company Template selection;
- one Company-level model-provider setup;
- connector-style Tool authorization;
- guided employee creation and automatic qualification;
- Goal-first operation;
- concise approval and result views.

### Advanced mode

- Agent Definitions and Versions;
- MCP/A2A Registry and credential bindings;
- Feature Gates and Policies;
- Task/Subtask/Run/Attempt details;
- custom Position, Operation, Pack, budget, quota, and Memory authoring.

Feature Gates remain deployment configuration. Ordinary users select a product mode or Template;
they do not manually construct a comma-separated feature configuration.

## Business-neutral extension architecture

AgentMesh core owns reusable control-plane primitives:

- identity, Registry, Employee projection, Position and Appointment;
- Task, Run, Attempt, Handoff, Planner, Scheduler, and Team Composer;
- Goal Contract and generic custom-resource/controller contracts;
- Business Object and Artifact substrate;
- Memory lifecycle and adapter ports;
- MCP, A2A, Credential, Policy, Approval, Budget, and audit;
- Pack trust, installation, upgrade, and compatibility;
- generic Admin and Office extension hosts.

A scenario extension owns:

- departments, Positions, and responsibility contracts;
- domain resource schemas and lifecycle Conditions;
- Agent Role Blueprints and qualification fixtures;
- declarative Operations/workflows;
- domain projection mappings;
- Tool requirements and connector guidance;
- domain Memory and Policy defaults;
- evaluations and examples;
- optional Controller code and UI contribution.

The core must not accumulate market-research, software-delivery, customer-support, sales, legal,
or accounting behavior.

## Pack and Controller model

### Declarative Pack

A safe Pack may contain:

```text
pack/
|- agentmesh-pack.yaml
|- resources/
|- roles/
|- workflows/
|- projections/
|- policies/
|- memory/
|- evaluations/
|- office/
`- README.md
```

The Pack declares compatible AgentMesh versions, dependencies, required Features, required logical
Tools, credential needs, permissions, migration behavior, digests, and provenance. Installation
previews every mutation and authority requirement. Declarative Packs do not execute arbitrary
code inside the API process.

### External Controller

Complex domains may ship an independently deployed Controller. The Controller:

- watches versioned domain resources through a public API;
- acquires a bounded reconciliation lease;
- compares desired Spec with observed Status and Conditions;
- creates or reuses Tasks using stable operation keys;
- observes results and evidence;
- proposes or applies allowed Status transitions;
- emits safe diagnostics;
- never receives database credentials;
- never treats model output as authorization;
- stops at budget, retry, deadline, Policy, or Approval boundaries.

Controller failure must not corrupt core execution. AgentMesh retains authoritative resources,
leases, Tasks, evidence, and last observed Status.

### Result projection

Simple output-to-object transformations are declarative and schema-validated. Executable
projection logic belongs to a Controller. The current market-research materialization service is
the reference behavior to migrate: bounded output contract, real Tool Invocation evidence,
idempotent Business Objects/Artifact creation, draft lifecycle, and explicit retry.

## Repository and community evolution

The project does not create many empty repositories before extension contracts work. Recommended
sequence:

1. keep the current repository as the integration laboratory;
2. define versioned Pack, resource, Controller, projection, UI, and compatibility contracts;
3. make Market Intelligence use only those public contracts;
4. publish an SDK and cross-repository contract/E2E suite;
5. create a GitHub organization with a small number of maintained repositories;
6. migrate the Market Intelligence reference Pack;
7. retain only a minimal compatibility fixture in core.

Initial repository set after step 4:

```text
agentmesh/agentmesh                    # business-neutral control plane
agentmesh/agentmesh-sdk                # Pack/Controller clients and test kit
agentmesh/pack-market-intelligence     # first real virtual company
agentmesh/agentmesh-rfcs               # cross-repository contracts and decisions
```

Marketplace and additional official Packs are created only after at least two independent Packs
exercise the same stable contracts.

## Compatibility and migration

- Existing low-level Task, Registry, Company, MCP/A2A, and Artifact APIs remain supported.
- Employee APIs initially orchestrate existing resources rather than replace them.
- Existing Agent Definitions can appear as employees after an explicit Company Appointment.
- Existing Company employees receive generated presentation profiles without changing identity.
- Market Intelligence remains built in until an external Pack passes the same offline, PostgreSQL,
  restart, security, and E2E fixtures.
- Scenario-specific routes and Console forms become compatibility facades, then move behind Pack
  contributions after a documented deprecation window.
- No migration rewrites historical Agent Version, Appointment, Task, Run, Memory, or Artifact
  lineage.

## Delivery plan

### Slice 0 - contract and product-language alignment

- [ ] adopt Company/Employee/Goal/Approval/Result terminology in the primary UI;
- [ ] define the EmployeeProfile projection contract;
- [ ] define Role Blueprint and simplified Employee creation contracts;
- [ ] define evidence-backed employee metrics and development invariants;
- [ ] document core versus scenario ownership and repository migration gates.

### Slice 1 - simple employee experience

- [ ] add Employee list/profile APIs over current resources;
- [ ] add a Company-default provider policy that references credentials without copying secrets;
- [ ] create Employee from a Role Blueprint with transactional qualification/Appointment;
- [ ] add Office Employee creation and profile panels;
- [ ] provide actionable readiness Conditions instead of raw subsystem errors;
- [ ] preserve the Advanced Registry experience.

### Slice 2 - goal and team composition

- [ ] add the simplified Goal request and status projection;
- [ ] implement deterministic explainable Team Composer eligibility and ranking;
- [ ] let Packs map a Goal/resource to a declarative coordinated workflow;
- [ ] launch Tasks through existing Goal Contract, budget, Policy, and scheduler services;
- [ ] expose team proposal, blockers, progress, Approval, and Result in the Office.

### Slice 3 - employee development

- [ ] implement Development Plan lifecycle and budget;
- [ ] add qualification/training Task templates and independent evaluation;
- [ ] propose Memory, capability, Version, Tool, model, and Appointment changes;
- [ ] require explicit authorization for every authority-increasing change;
- [ ] expose evidence-backed history and development recommendations.

### Slice 4 - extension runtime

- [ ] define versioned custom resource Spec/Status/Condition contracts;
- [ ] define reconciliation lease, idempotency, retry, and failure contracts;
- [ ] implement declarative workflow and result projection definitions;
- [ ] add Controller API/SDK and local deterministic test harness;
- [ ] migrate research materialization to the generic contract;
- [ ] prove two structurally different reference Packs.

### Slice 5 - repository extraction

- [ ] publish SDK compatibility fixtures and supported-version policy;
- [ ] move Market Intelligence to its own repository without internal imports;
- [ ] validate cross-repository install, upgrade, restart, and E2E paths;
- [ ] create the community organization/repositories and contribution governance;
- [ ] reduce the core scenario to a minimal compatibility example.

## Acceptance criteria

- A new owner completes the deterministic first Goal in less than ten minutes without reading
  control-plane documentation.
- Standard setup requires no more than one model-provider connection plus the Tools explicitly
  required by the selected Template.
- Creating an employee produces one stable Agent Definition, one qualified immutable Version, one
  auditable Appointment, a governed Memory namespace, and an Office profile without partial active
  state.
- Changing models, instructions, Tools, or Position preserves employee identity and historical
  lineage.
- Every displayed capability, performance statement, and development outcome links to persisted
  evidence and includes scope/sample size where relevant.
- Memory or performance cannot independently grant Tool, budget, approval, capability, or Position
  authority.
- A Goal can automatically produce an explainable eligible team and a governed Task plan.
- Every non-ambient Office collaboration animation corresponds to a persisted event.
- A scenario Pack installs without adding industry code to core.
- An external Controller can crash and restart without duplicating Tasks, Results, or external
  actions.
- Market Intelligence runs from an external repository using only public contracts before its
  built-in implementation is removed.
- Demo, Standard, and Advanced modes operate against the same authoritative resources and audit
  trail.

## Success measures

Product measures:

- time to first completed Goal;
- setup completion and actionable-blocker rate;
- percentage of Goals created through the simple surface;
- percentage of required approvals resolved without opening raw Task internals;
- Result acceptance and revision rates;
- employee development proposals accepted/rejected with evidence;
- number of independently maintained Packs that pass compatibility tests.

Reliability and safety measures remain authoritative:

- duplicate Task/Result/external-action rate;
- recovery after API, Worker, Controller, Redis, or provider interruption;
- unauthorized Tool/Memory/credential access attempts rejected;
- unbounded retry or budget escape rate;
- Office projection inconsistencies;
- evidence completeness for capability, performance, approval, and Result claims.

## Non-goals

- simulating consciousness, emotion, friendship, morale, or legal employment;
- fictional XP, levels, or promotion without evidence;
- autonomous self-modification or self-granted authority;
- exposing every control-plane resource to ordinary users;
- replacing specialized Agent frameworks used behind an Employee runtime adapter;
- embedding third-party Controller code inside the trusted API process;
- creating a large marketplace or many repositories before compatibility contracts stabilize;
- unrestricted external publication, spending, contracting, outreach, or regulated decisions;
- hiding audit detail from operators in the name of simplicity.

## Relationship to existing proposals

- [Virtual Company operating model](virtual-company-operating-model.md) defines the existing
  Company, Position, Appointment, Goal, Operation, and Pack foundation.
- [Organizational memory service](organizational-memory-service.md) defines governed Employee
  continuity and reviewed learning.
- [AgentMesh Office game-world evolution](agentmesh-office-game-world.md) defines truthful spatial
  presentation.
- [Company operations and business objects](company-operations-and-business-objects.md) defines
  recurring business work and durable domain records.
- [External memory adapters](external-memory-adapters.md) keeps optional Memory engines behind
  AgentMesh authority.

This proposal connects those foundations into one employee-first product and establishes the
extension boundary required to grow a multi-repository community safely.
