# Company Operations implementation

Status: implemented baseline.

## Outcome

The `company_operations` feature turns an approved recurring or manual business operation into
normal AgentMesh Tasks without bypassing Task admission, execution, governance, or evidence.
PostgreSQL remains the source of truth for the Operation, trigger cursor, occurrence, Task link,
and any dispatch exception.

## Runtime contract

An Operation belongs to one Company and active Organization Unit. It freezes the objective/input
template, optional Initiative and Position bindings, trigger definition, missed-schedule policy,
run-window and concurrency limits, and governance metadata behind a SHA-256 content digest.

The implemented trigger types are:

- deterministic interval schedules with timezone metadata;
- idempotent manual/business-event triggers carrying a stable external event ID.

The schema is extension-ready for calendar, cron, cycle-relative, and typed Business Object event
triggers, but those trigger calculators are not advertised as implemented.

Every scheduled or external event derives this key:

```text
operation:{operation_id}:version:{version}:occurrence:{scheduled_at_or_event_id}
```

The database enforces one occurrence per key. The same key is also passed to atomic Task-creation
idempotency, so scheduler redelivery cannot create another Task.

## Concurrency and recovery

`SELECT ... FOR UPDATE SKIP LOCKED` claims due trigger-state rows. The transaction advances the
deterministic cursor and increments its fencing token before Task creation. A durable pending
occurrence bridges that transaction boundary and makes a crash or retry safe.

Dispatch enforces:

- Operation active state;
- Task concurrency limit;
- maximum created runs in a configured time window;
- `SKIP`, `LATEST`, `CATCH_UP_BOUNDED`, and `REQUIRE_REVIEW` missed-run policies;
- at most three Task-creation attempts, followed by a terminal operator-visible exception.

Failures never manufacture Task success. Each retry decision and final exhaustion remains visible
through the Operation snapshot API.

## Interfaces

Company-scoped APIs under `/api/v1/companies/{company_id}/operations` create, list, inspect,
activate, pause, disable, manually trigger, and dispatch due Operations. The snapshot includes the
trigger cursor, occurrence/Task lineage, skipped/review evidence, and exceptions.

`POST /api/v1/companies/{company_id}/operations/_activate/staffed` is the guarded activation path
for Position-bound Operations. It locks the selected Operations, verifies every active
Appointment, published default Agent Version, and required capability before changing any
Operation. A failed preflight changes nothing. Repeating activation for an already-active,
still-valid Operation is safe.

When an Operation has Position bindings, an occurrence creates a `COORDINATED` Task rather than a
generic direct Task. Each bound Position becomes one Subtask with its responsibility contract,
required capabilities, preferred Agent, Appointment ID, and appointed Agent-Version evidence.
The Task's `company_context.workforce` preserves the same lineage for inspection and replay.
The normal Task run transition remains explicit and all existing admission, policy, Tool, and
approval boundaries continue to apply.

Run `agentmesh-company-operations` for continuous polling. The optional Compose service is enabled
with the `company` profile after explicitly enabling `company_model`, `company_goals`, and
`company_operations`.

## Current boundary

Typed Business Objects, governed organizational Memory, financial ledgers, and Company Packs are
separate feature-gated modules. Calendar and cron triggers should be added through deterministic
calculators with timezone/DST fixtures, not by accepting arbitrary scheduler code. Appointment
replacement/termination remains an explicit Company-model action; the template wizard does not
silently replace an occupied Position.
