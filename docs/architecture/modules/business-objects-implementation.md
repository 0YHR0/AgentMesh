# Typed Business Objects implementation

Status: implemented baseline.

## Outcome

The `business_objects` feature gives a Company durable typed records for customers, leads,
opportunities, campaigns, deliverables, support cases, invoices, or domain-specific work without
hard-coding those concepts into AgentMesh core.

## Type contract

A `BusinessObjectType` combines:

- a Draft 2020-12 JSON Schema;
- an initial lifecycle state and declared states;
- named actions with source/target states and input schemas;
- an explicit allowlist of fields each action may update;
- required Position keys, Agent capabilities, and evidence;
- sensitive top-level fields, ownership rules, and retention metadata;
- a schema version and immutable SHA-256 content digest.

Types move from `DRAFT` to `PUBLISHED` and may be `DEPRECATED`. Publishing a higher schema version
deprecates the prior published version. Existing Objects remain pinned to their original Type ID
and schema version; there is no silent data migration.

## Object and revision contract

Creating an Object validates its complete data against the published Type schema and optionally
binds an active Company Position. Every Object begins at revision 1 and its declared initial
lifecycle state.

Agents and users cannot submit arbitrary JSON patches. They invoke a named action with:

- the expected current revision;
- action input conforming to its schema;
- actor/source identity;
- required Position/capability declarations;
- evidence references when required.

The service locks the Object, rejects stale expected revisions, validates lifecycle admission and
field allowlists, validates the resulting complete object, then atomically appends a revision,
updates the Object cursor, and emits a redacted Outbox event. PostgreSQL optimistic versioning is a
second guard against concurrent updates.

Revisions are append-only and retain the original data digest, source, actor, action, and evidence
references. API snapshots redact declared sensitive top-level fields without changing their
stored digest. Domain events contain only IDs, type/version, lifecycle state, digest, and evidence
count.

## Safety boundary

An action declaring a side-effect class other than `NONE` is rejected until a governed external
action adapter supplies Policy approval, idempotency, and reconciliation. Object state therefore
cannot imply that an invoice was sent, a payment happened, or a customer was contacted merely
because an Agent returned text.

The current baseline supports user-authored Types. Pack installation, compatibility previews,
nested field-level data policies, financial constraints, event-triggered Operations, and retention
execution are separate modules.

## Interfaces

Company-scoped APIs under `/api/v1/companies/{company_id}` expose:

- Business Object Type create/list/publish/deprecate;
- Object create/list/get;
- named action application;
- redacted append-only revision timelines.

Enable with:

```dotenv
AGENTMESH_FEATURE_GATES=company_model=true,business_objects=true
```

The feature depends only on `company_model`, so manual typed records do not require recurring
Operations.
