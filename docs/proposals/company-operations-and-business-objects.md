# Company operations and business objects

Status: Implemented baseline (`company_goals`, `company_operations`, and `business_objects`);
calendar/event trigger extensions, Company Metrics, and Pack-managed definitions remain proposed.

## Outcome

A Virtual Company must operate continuously rather than waiting for the owner to manually create
every Task. This proposal adds an authorized Company Operations Engine that converts approved
objectives, schedules, and business events into bounded AgentMesh Tasks.

The engine also introduces a typed Business Object Registry so Agents work on customers,
opportunities, campaigns, deliverables, orders, invoices, and support cases instead of treating all
business activity as unstructured chat.

## Boundary

The Operations Engine decides **when a permitted unit of work is due**. The existing AgentMesh
runtime decides how that Task is admitted, scheduled, executed, governed, observed, and recovered.

The engine may create Tasks through the same application command used by an authorized operator.
It may not:

- directly create Runs or Attempts;
- bypass Task budget and quota admission;
- infer that an external event occurred without a persisted event;
- mark an Objective or Key Result complete from Agent narrative;
- retry unknown external side effects without reconciliation.

## Strategy objects

### Operating cycle

```text
OperatingCycle
- id
- company_id
- name
- starts_at
- ends_at
- status
- approved_by
- approved_at
- review_schedule
```

Lifecycle:

```text
DRAFT → APPROVED → ACTIVE → REVIEWING → CLOSED
                 ↘ PAUSED
```

Only one active default cycle is required in the first implementation.

### Objective

```text
Objective
- id
- cycle_id
- owner_position_id
- statement
- rationale
- status
- priority
- target_date
```

An Objective describes an outcome, not a list of activities.

### Key result

```text
KeyResult
- id
- objective_id
- metric_definition_id
- baseline
- target
- current_verified_value
- current_estimated_value
- measurement_source
- status
```

Verified and estimated values are always separated. An Agent forecast cannot overwrite imported or
reconciled measurement evidence.

### Initiative

```text
Initiative
- id
- objective_id
- owner_unit_id
- title
- outcome_contract
- budget_allocation_id
- status
- starts_at
- ends_at
```

Tasks link to an Initiative. Closing an Initiative requires acceptance evidence and does not
automatically imply that the Objective or Key Result succeeded.

## Recurring operation

An Operation is an approved template for bounded repeatable work:

```text
Operation
- id
- company_id
- organization_unit_id
- key
- name
- objective_template
- task_template_id
- trigger_definition_id
- position_bindings
- tool_capability_allowlist
- memory_policy_id
- budget_limit
- concurrency_limit
- maximum_runs_per_window
- approval_policy_id
- status
- version
- content_digest
```

Examples:

- daily lead qualification;
- weekly market report;
- hourly support-ticket triage;
- nightly financial reconciliation;
- monthly customer health review;
- repository dependency audit.

Editing an Operation creates a new version. Already created Tasks retain the prior version digest.

## Triggers

Supported trigger classes:

### Schedule

- interval;
- daily local time;
- weekly day/time;
- bounded cron expression;
- cycle-relative milestone.

All schedules store an IANA timezone and calculate a deterministic next due time. Daylight-saving
changes are explicit in schedule tests.

### Business event

- Business Object created or transitioned;
- imported webhook accepted;
- KPI threshold crossed;
- Task or Initiative completed;
- approval resolved;
- external reconciliation changed state.

An event Trigger consumes a versioned, idempotent envelope. Re-delivery does not create duplicate
Tasks.

### Manual

An authorized operator may trigger a run immediately while preserving the Operation version and
budget boundary.

## Trigger state

```text
OperationTriggerState
- operation_id
- trigger_version
- next_due_at
- last_evaluated_at
- last_fired_at
- last_event_id
- consecutive_failures
- paused_reason
- fencing_token
```

Due triggers are claimed with database locking and a lease. Multiple schedulers cannot create the
same occurrence twice.

Stable occurrence key:

```text
operation:{operation_id}:version:{version}:occurrence:{scheduled_at_or_event_id}
```

The key becomes the Task creation idempotency key.

## Missed schedules and backpressure

Each Operation chooses a policy:

- `SKIP`: discard missed occurrences and record evidence.
- `LATEST`: create only the newest missed occurrence.
- `CATCH_UP_BOUNDED`: create up to a configured maximum.
- `REQUIRE_REVIEW`: pause and ask an operator.

Budget exhaustion, disabled Tools, missing Agent appointments, quota pressure, or unhealthy
dependencies do not produce infinite retry loops. They create an `OperationException` with bounded
backoff and an operator-visible remediation.

## Business Object Registry

Business Object Types are contributed by versioned `BusinessObjectPack` resources. AgentMesh core
validates their schemas, lifecycle transitions, named actions, ownership, and compatibility, but it
does not know CRM, order, invoice, or industry-specific fields. Installing a Pack must preview its
types, migrations, Policy dependencies, and connector requirements before registration.

### Object type

```text
BusinessObjectType
- id
- company_id
- key
- schema_version
- json_schema
- lifecycle_definition
- sensitive_fields
- ownership_rules
- retention_policy
- status
- content_digest
```

### Object

```text
BusinessObject
- id
- company_id
- type_id
- external_ref
- current_revision
- status
- owner_position_id
- created_at
- updated_at
```

### Revision

```text
BusinessObjectRevision
- object_id
- revision
- data
- data_digest
- source_type
- source_id
- actor
- created_at
```

Revisions are append-only. Optimistic concurrency prevents an Agent from overwriting a newer
customer or financial state.

## Initial object types

The Registry supports extensions, but the first template Packs may define:

| Object | Purpose |
|---|---|
| `customer` | durable customer identity and non-secret profile |
| `lead` | prospect qualification state |
| `opportunity` | potential commercial engagement |
| `offer` | bounded product/service and price proposal |
| `campaign` | coordinated marketing activity |
| `deliverable` | promised digital output and acceptance state |
| `support_case` | customer issue and resolution evidence |
| `invoice` | amount due and external accounting reference |
| `expense_request` | proposed company expense |
| `payment_request` | proposed payment requiring governance |
| `business_metric` | imported or derived metric observation |

Financial types receive additional constraints from the financial-governance proposal.

## Object actions

Agents do not directly patch arbitrary JSON. An Object Type defines named actions:

```text
qualify_lead
advance_opportunity
attach_deliverable
request_invoice
resolve_support_case
propose_expense
record_metric_observation
```

Each action specifies:

- required current lifecycle state;
- allowed Position/capability;
- input schema;
- validation;
- required evidence;
- side-effect class;
- approval action type;
- resulting state;
- emitted event.

The application service applies the action atomically and appends the new revision and Outbox
event.

## Metrics

```text
MetricDefinition
- id
- company_id
- key
- unit
- aggregation
- source_policy
- verification_policy
- freshness_window
```

```text
MetricObservation
- id
- metric_definition_id
- period
- value
- evidence_type
- evidence_id
- verification_status
- observed_at
```

Values may be:

- `VERIFIED`: imported from an authorized source or reconciled ledger;
- `ESTIMATED`: calculated or forecast by an Agent;
- `PROPOSED`: awaiting review;
- `REJECTED`.

Dashboards must display the classification.

## Company event model

The existing MessageEnvelope carries company events:

```text
company.object.created
company.object.transitioned
company.metric.observed
company.operation.due
company.operation.fired
company.operation.blocked
company.cycle.review_due
```

Events contain IDs, revisions, and redacted metadata. They do not carry customer communications,
credentials, complete contracts, or payment details.

Redis Streams remains a delivery mechanism; PostgreSQL records the authoritative occurrence,
object revision, and Task link.

## Operation-generated Task

The generated Task binds:

- Company, Organization Unit, Operation ID/version/digest;
- triggering occurrence or event ID;
- Objective/Initiative if applicable;
- involved Business Object IDs and revisions;
- appointed Agent Versions for role slots;
- Goal Contract and acceptance criteria;
- budget, deadline, concurrency, and maximum revisions;
- Memory Policy and allowed namespace scopes;
- Tool and Policy requirements.

The Task output never silently mutates objects. A successful Task may submit one or more typed
Object Action commands, which are validated independently.

## Console and Office

Console surfaces:

- cycles, Objectives, Key Results, and Initiatives;
- Operation schedule, next due time, status, budget, and exceptions;
- object timeline and revisions;
- verified versus estimated metrics;
- generated Task links;
- pause, resume, trigger-now, disable, and remediation actions.

Office surfaces:

- organization-unit objective boards;
- recurring work queues;
- object-backed work items;
- exception and approval indicators;
- event-backed movement only.

The Office never displays an Agent-generated forecast as actual revenue or progress.

## Feature gates

- `company_goals`
- `company_operations`
- `business_objects`
- `company_metrics`

Operations depend on `company_model`, existing Task execution, Policy, and Event Relay. Business
Objects may be used manually before Operations are enabled. Pack-managed definitions additionally
require the proposed `company_packs` gate; user-authored definitions may remain a separate,
explicitly governed authoring path.

## Delivery slices

### Slice 1 — manual goals and objects

- [x] Operating Cycle, Objective, Key Result, Initiative, and Task lineage;
- [x] versioned Object Types and append-only Objects;
- [x] manual direct Task creation linked to an active Initiative;
- [x] exact Key Result observations with verified/estimated separation;
- [x] deterministic API tests and redacted revision projection.

### Slice 2 — scheduled operations

- [x] versioned interval schedules (daily/weekly calculators remain an extension);
- [x] leased due-trigger worker;
- [x] idempotent Task creation;
- [x] pause, resume, disable, trigger-now, and missed-schedule policy;
- [x] concurrency and run-window preflight (deeper budget/Tool/appointment preflight remains).

### Slice 3 — event triggers

- object transition and metric threshold events;
- Inbox deduplication and stable occurrence keys;
- bounded backoff and operation exceptions;
- webhook adapter boundary without shipping provider-specific webhooks.

### Slice 4 — management review

- cycle review Tasks;
- KPI evidence bundle;
- proposed next-cycle adjustments;
- candidate organizational learning;
- Office boards and recurring work queues.

## Acceptance criteria

- Re-delivering a schedule occurrence or business event creates at most one Task.
- Two scheduler instances cannot both claim the same occurrence.
- An Operation cannot bypass Task quota, budget, Tool, Policy, or appointment checks.
- Missed schedules follow their configured policy after downtime.
- Business Object revisions reject stale writes and preserve history.
- A Task output cannot mutate an Object without a valid named action.
- Verified and estimated metrics remain distinguishable throughout API, Console, export, and
  Office.
- Disabling an Operation prevents future occurrences without cancelling already created Tasks.
- All slices run deterministically without paid APIs or external services.
- Two Domain Packs can register unrelated object and Operation models without adding fields or
  branching logic to the AgentMesh core.

## Non-goals

- a general no-code workflow engine;
- arbitrary cron-triggered shell execution;
- replacing CRM, accounting, ERP, ticketing, or marketing systems;
- interpreting model prose as a business-object transition;
- automatic achievement of an Objective because Tasks were completed;
- unlimited catch-up after downtime;
- unsupervised external side effects.
