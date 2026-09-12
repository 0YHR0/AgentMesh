# Managed Runtime reviewed/coordinated cutover

Status: implementation-ready design baseline  
Milestone: A4.2  
Depends on: A4.0, A4.1a, A4.1b.1, A4.1b.2, Managed Agent Runtime API v0.1  
Tracks: #135, #136, #154

## 1. Purpose

A4.2 moves newly admitted `REVIEWED` and `COORDINATED` work from the legacy
`WorkflowRunner` authority to the same Managed Agent Runtime authority already proven for bounded
`DIRECT` Runs. It preserves the existing reviewed revision policy and coordinated DAG semantics;
the Runtime replaces only the framework-specific execution boundary.

This is not a gate broadening exercise. Reviewed and coordinated Tasks create additional Runs after
the first Run has completed. Coordinated Tasks can also have several active Runs at once. A safe
cutover therefore requires:

- one immutable authority cohort for every multi-Run Task;
- canonical work-item snapshots that are identical on legacy and managed paths;
- one business-outcome applier shared by both execution authorities;
- mode-aware unknown-outcome parking and reconciliation;
- no new DAG scheduling while any coordinated Runtime outcome is unresolved;
- explicit handling of active, queued, and late sibling Runs;
- independent gates and rollback for reviewed and coordinated admission.

The slice remains CI/test-only. A4.2 implements the lifecycle control-plane protocol and
deterministic provider proof needed for safe admission. Production-durable provider reattach,
lifecycle workers/backends, full chaos qualification, and removal of the legacy Worker remain
A4.3.

## 2. Non-goals

A4.2 does not:

- change review acceptance criteria, revision limits, DAG dependency rules, or Agent selection;
- let a Runtime create a reviewer, revision, Subtask, Supervisor, Task, Run, or Attempt;
- mix Runtime Versions inside one reviewed/coordinated authority cohort;
- enable managed cutover in a production environment;
- claim that the current in-process LangGraph provider state is restart durable;
- add reviewed policy to coordinated Tasks;
- migrate federated A2A Tasks;
- accept usage-bearing managed terminal observations until pricing lineage is designed;
- materialize arbitrary Runtime output Artifact references as business output in this slice.

## 3. Normative invariants

### 3.1 Authority cohort

1. Every Run still persists its own `runtime_authority`, `runtime_version_id`, and
   `runtime_execution_intent_id`.
2. The first Run of a reviewed or coordinated Task chooses the cohort from the relevant admission
   gate. Every later reviewer, revision, Subtask, and Supervisor Run inherits that cohort; later
   gate changes are ignored.
3. A reviewed/coordinated Task may not contain both `legacy` and `managed` Runs. A mismatch is an
   integrity error, not a reason to choose the newest gate value.
4. A managed cohort pins one Runtime Version. Later Runs reuse that version. `DEPRECATED` remains
   usable for the active cohort; `REVOKED`, missing, or incompatible versions fail closed before a
   new Run is persisted.
5. Every managed Run receives a new stable Runtime execution-intent UUID. Attempt replacement does
   not change it.
6. Comparison mode remains separate from authority cutover and is not enabled for orchestrated
   Runs in A4.2.

The cohort is derived from immutable persisted Runs rather than a duplicated mutable Task field:

- no prior Run: evaluate the mode-specific gate and select the built-in LangGraph v2 Runtime;
- prior Runs: lock/read all Task Runs, require one authority and, for managed Runs, one Runtime
  Version, then inherit it;
- an explicit parent Run is additionally checked for reviewed reviewer/revision lineage;
- a coordinated scheduling transaction computes the cohort once and passes it to every Run it
  creates.

Initial cohort selection locks and accepts only a `PUBLISHED` Runtime Version. Inheritance locks the
same Runtime Version row through child Run and Outbox commit and accepts `PUBLISHED` or
`DEPRECATED`; it rejects `REVOKED`. Runtime preparation must distinguish this pinned-cohort case
from first admission instead of applying the current PUBLISHED-only lookup unconditionally. A
concurrent revoke either wins before the child transaction and prevents Run creation, or commits
after the already-pinned child; dispatch then rechecks revocation and fails safely before provider
contact. REVIEWED/COORDINATED requests explicitly reject `deterministic_shadow` in A4.2.

This avoids a migration merely for rollout state while making historical Runs the authoritative
snapshot. A future heterogeneous Runtime planner may replace the one-version cohort with an
immutable per-work-item Runtime plan; it must not weaken the no-gate-re-evaluation rule.

Every Run creation site must use the cohort resolver. The mandatory inventory is:

- initial DIRECT/REVIEWED request in `TaskApplicationService.request_run`;
- reviewer and revision creation after known or reconciled reviewed output;
- Subtask and Supervisor creation in `CoordinatedScheduler`;
- reviewer/revision replacement and coordinated resume in `TaskResolutionService`;
- safe recreation of a never-dispatched coordinated sibling after reconciliation.

There is no permitted direct `TaskRun.request(...)` bypass at the listed local
DIRECT/REVIEWED/COORDINATED creation sites. Federated/A2A Run creation remains outside A4.2. Tests
scan the local sites and prove that gate-off legacy behavior remains unchanged.

If an inherited Runtime Version is unavailable or `REVOKED`, the predecessor Run/Attempt result
remains durably successful but no child Run is created. The Task fails atomically with
`runtime.version_unavailable`; it may not silently migrate the cohort to another Runtime Version.
This bounded CI cutover deliberately prefers a truthful terminal failure over introducing a new
operator-migration workflow. A future migration command requires its own versioned design and
audit trail.

### 3.2 Ownership

- PostgreSQL Task/Run/Subtask/Attempt records remain business authority.
- Runtime observations are evidence and never directly schedule a reviewer, revision, Subtask, or
  Supervisor.
- The application outcome applier performs those transitions after identity, fence, result,
  budget, policy, and mode validation.
- Adapter calls remain outside database transactions.
- Managed Runtime paths never call the legacy authoritative runner in parallel or as fallback.
- Once the dispatch boundary is crossed, an unknown outcome never creates another execution.

### 3.3 Terminal contract

All authoritative managed modes use one terminal validator:

- phase is one of `SUCCEEDED`, `FAILED`, `CANCELED`, `TIMED_OUT`, `LOST`, or `OUTCOME_UNKNOWN`;
- terminal observations have empty `usage`, `governed_action_requests`, and `wait_refs` in A4.2;
- success has mapping output and no error; bounded output Artifact references are retained as
  Runtime evidence but are not materialized into the Artifact ledger or substituted for mapping
  output in A4.2;
- non-success has no output or output Artifact references;
- execution, Assignment, Runtime Version, current Attempt, and fence identities match exactly;
- requested cancellation is distinguished from an unsolicited provider cancellation;
- malformed or contradictory evidence fails closed before business mutation.

This closes #154 before either new cutover gate can admit a Run.

Because validation occurs after a provider dispatch may already have crossed, "fails closed" has a
specific meaning. A contradictory terminal observation is not converted into an ordinary Task
failure and is not thrown back to message retry. The control plane atomically:

1. retains the provider observation as bounded conflict evidence;
2. records a synthetic fenced `OUTCOME_UNKNOWN` control-plane observation with
   `runtime.terminal_contract_invalid`;
3. conservatively settles the Attempt, releases quota once, and parks the applicable business
   state for reconciliation;
4. consumes the Inbox message and emits the reconciliation-required event.

The provider is never redispatched. A later privileged command needs new, canonical, identity-bound
evidence to converge the outcome. Pre-dispatch Assignment/validation failures retain the existing
safe ordinary failure behavior because no provider side-effect boundary was crossed.

Implementation must not pass the contradictory phase through the ordinary observation writer,
because that writer would advance RuntimeExecution before the shape conflict is known. Extend the
managed result with an optional bounded `conflicting_observation` and make identity/terminal
validation run immediately after bounded structural decoding. The execution service returns a synthetic unknown
observation plus the original conflict; finalization uses a dedicated
`record_conflicting_observation_in_uow` evidence path that is forced to `CONFLICT` and cannot mutate
RuntimeExecution, followed by the ordinary fenced synthetic observation. Both records and parking
commit in one UoW. The forced-conflict API accepts no caller-selected processing outcome and is not
exposed as a public endpoint.

`conflicting_observation` is an application-owned safe envelope, not an arbitrary provider object.
For a structurally decoded `RuntimeObservation` it contains the canonical raw observation digest,
a deterministic control-plane observation ID derived from expected RuntimeExecution + digest,
the decoded phase/time/sequence, and boolean mismatch categories only. The persisted observation
uses the expected execution and Assignment identities; claimed foreign IDs, output, error text,
provider bodies, and other raw fields are not copied into its public projection or evidence JSON.
For an object that cannot be bounded and canonicalized, the envelope instead uses a deterministic
digest over `{expected_execution_id, runtime.terminal_contract_invalid, structural_invalid=true}`
and no provider-derived identity or body. Thus malformed evidence still leaves a safe conflict
marker without turning attacker-controlled identifiers into cross-tenant metadata.

The forced writer locks the expected RuntimeExecution, verifies the current Attempt/fence, and
always persists `processing_outcome=CONFLICT`; it has no code path that calls
`RuntimeExecution.apply_observation`. Exact digest replay under that execution reuses the existing
conflict marker. A different digest creates another conflict record. The ordinary synthetic
`OUTCOME_UNKNOWN` observation is written only after that marker and is the sole observation allowed
to advance the Runtime in this transaction.

The application DTO for this path is `ManagedRuntimeConflictObservation`; it is immutable and has
no raw-object or caller-selected-outcome field. Its closed fields are:

| Field | Contract |
|---|---|
| `observation_id` | UUIDv5 derived from the expected execution ID plus the conflict digest |
| `observation_digest` | canonical digest of a bounded decoded observation, or the static malformed marker digest |
| `phase`, `observed_at`, `provider_sequence` | decoded bounded values; malformed input uses `OUTCOME_UNKNOWN`, the expected execution timestamp, and `None` |
| `structural_invalid` | true only when no bounded decoded `RuntimeObservation` is available |
| `execution_id_mismatch`, `assignment_id_mismatch`, `assignment_digest_mismatch` | booleans computed against expected persisted identities |
| `terminal_contract_invalid`, `protocol_error_observation` | closed semantic mismatch booleans |

The conflict writer accepts that DTO plus expected execution, Attempt, and fence only. In fixed
order it locks the expected execution, verifies tenant/owner/fence and expected Assignment binding,
looks up prior rows by the deterministic ID or digest, returns only an exactly matching existing
`CONFLICT` row, rejects any identity/outcome/byte collision, rejects a new pre-terminal marker for
an already-terminal execution, and otherwise appends one safe `RuntimeObservationEvidence` row.
The row is bound to expected identities, has `provider_event_present=false`, a static summary, and
an evidence JSON object containing exactly the six booleans above. The method returns the stored
evidence row, never accepts a processing outcome argument, never emits an event by itself, and
never saves or applies a RuntimeExecution. B2 owns the surrounding transaction and side effects.

For B2, `ManagedRuntimeAuthoritativeResult` carries the normal terminal observation plus an
optional safe conflict DTO. The execution coordinator sets it only when a provider candidate was
available but failed the terminal contract; the normal observation is then the deterministic
control-plane `OUTCOME_UNKNOWN` observation. Provider-call uncertainty with no candidate has no
fabricated conflict DTO. Finalization revalidates all persisted execution/Assignment/owner facts,
records the optional conflict first, then records the exact synthetic unknown through the ordinary
writer, then parks and settles business state, writes reconciliation Outbox and Inbox rows, and
commits once. If finalization itself encounters an invalid candidate without a supplied envelope,
it derives the same safe DTO locally before replacing the candidate with synthetic unknown. A
supplied conflict alongside any non-canonical synthetic unknown is invalid control-plane state.
Any failure at either evidence write or any later settlement step rolls back both observations,
Runtime phase, Task/Run/Attempt/accounting, Inbox, and Outbox together.

A structurally valid observation with the wrong execution/Assignment identity follows the same
path. It is bound only to the expected execution from the dispatch context; the evidence record
stores the raw canonical observation digest plus bounded mismatch flags, never looks up or mutates
the caller-claimed execution, and never exposes cross-tenant identity details. A malformed DTO that
cannot be canonicalized is represented only by a static safe protocol-conflict reason and the
synthetic unknown observation.

### 3.4 A conflicting terminal after business commit

The synthetic-unknown parking rule applies only before a terminal Runtime observation has been
accepted and its business transition committed. A later second terminal observation cannot safely
rewind already-consumed business output or downstream effects.

For a RuntimeExecution that is already terminal, a different terminal observation is retained as
`CONFLICT` evidence and atomically opens a `CONFLICTING_TERMINAL` Runtime integrity incident. The
original Runtime phase and Task/Run business result remain frozen, automated consumers stop using
new evidence from that execution, and an integrity Outbox event is emitted. The incident links the
first accepted and later conflicting observation identities/digests/phases without storing raw
provider bodies.

Late-terminal recording is serialized by the locked RuntimeExecution. It resolves the accepted
terminal anchor from the existing `APPLIED` or `RECONCILED` known-terminal observation matching the
frozen execution phase. An exact replay of that accepted digest is a duplicate, not an incident.
A different terminal digest creates/reuses one conflict evidence record and one incident; only the
transaction that inserts the incident emits `agentmesh.runtime.integrity-incident.opened`. Missing
or contradictory accepted-terminal evidence is treated as control-plane corruption and fails
closed for operator repair; the implementation must not invent an accepted provider observation.

The A4.2a.1 implementation contract is deliberately internal and transaction-owned:

- `record_late_terminal_observation_in_uow(...)` accepts a caller-owned UoW, the expected execution
  identity, a typed `RuntimeObservation`, and one control-plane receipt timestamp. It has no public
  route, performs no commit, and is unavailable when managed Runtime is disabled.
- It first locks the tenant-scoped RuntimeExecution and requires its frozen phase to be exactly one
  of `SUCCEEDED`, `FAILED`, `CANCELED`, or `TIMED_OUT`. It then validates the candidate as a bounded
  known-terminal observation against the persisted execution and Assignment identities. `LOST`,
  `OUTCOME_UNKNOWN`, malformed values, wrong identities, and non-terminal values fail closed rather
  than opening an incident from untrusted or incomplete evidence.
- The repository provides a tenant-scoped accepted-anchor query, not an unbounded public list scan.
  The query returns only `APPLIED|RECONCILED` observations whose phase equals the frozen execution
  phase. Exactly one row is required. Zero rows or multiple rows fail closed as integrity
  corruption; the application does not choose an arbitrary anchor.
- The candidate is canonically digested and projected to a deterministic, bounded conflict identity.
  If its digest equals the accepted anchor digest, the method returns `DUPLICATE` without inserting
  evidence, an incident, or an Outbox event, even if the provider supplied another event identifier.
- For a different digest, the method first resolves exact prior conflict evidence by the derived
  observation identity or digest. Exact evidence replay is reused; any identity/digest collision
  with different safe fields fails closed. A new row has outcome `CONFLICT`, contains no provider
  body, and records the closed six-boolean safe envelope (all flags may be false because this path
  represents two individually valid but contradictory terminal results).
- Before insertion, the method resolves an incident by the exact tuple `(tenant, execution,
  accepted_digest, conflicting_digest)`. The repository returns both the semantic incident and a
  trustworthy `created` result (PostgreSQL uses `INSERT ... ON CONFLICT DO NOTHING RETURNING id`,
  never driver `rowcount`). The existing row must exactly match both observation identities,
  phases, reason, and immutable timestamps or replay fails closed.
- Only `created=true` appends `agentmesh.runtime.integrity-incident.opened`. The event contains safe
  execution/incident/observation identifiers, digests, phases, and status only. The conflict
  evidence, incident, and event share one UoW and one commit owned by the caller; any failure rolls
  all three back.
- The result explicitly distinguishes accepted replay, incident replay, and newly opened incident
  so callers cannot infer side effects from repository object identity. None of these branches save
  RuntimeExecution or mutate Task, Run, Attempt, accounting, output, Artifact, or Memory.

Acceptance tests freeze this boundary in both memory and real PostgreSQL: accepted-digest replay;
first different digest; exact conflict replay; a second distinct digest; missing/multiple anchors;
wrong tenant/identity/phase; immutable business and Runtime state; event payload redaction; and
failure injection after evidence, incident, and Outbox writes proving full rollback.

A4.2 exposes the incident for operator acknowledgement/escalation but does not rewrite the
business result. Changing a previously consumed result requires an explicit compensation design in
the Governed Action/Reliability track. Thus:

- conflict before terminal business commit -> `OUTCOME_UNKNOWN` parking and ordinary outcome
  reconciliation;
- conflict after terminal business commit -> immutable terminal result plus an open integrity
  incident, never silent overwrite.

The formal duplicate/ordering rule uses this split rather than claiming every late conflict can
transition a terminal RuntimeExecution back to `OUTCOME_UNKNOWN`.

The incident contract is closed for A4.2:

- identity: UUID plus tenant and RuntimeExecution; uniqueness is
  `(tenant_id, runtime_execution_id, accepted_observation_digest,
  conflicting_observation_digest)`;
- accepted and conflicting digests must differ, and `updated_at` must never precede `created_at`;
  these invariants are enforced by both the domain and PostgreSQL constraints. Because the reader
  floor was already deployed without those database checks, the following expand migration adds
  them fail-closed and never rewrites or deletes pre-existing incident evidence;
- status: `OPEN`, `ACKNOWLEDGED`, or `ESCALATED`. A4.2 has no `RESOLVED` transition because it has
  no compensation protocol;
- an exact repeated conflicting observation returns the existing incident and emits no second
  Outbox/audit side effect; a distinct conflicting digest creates a distinct incident;
- list/detail require `RUNTIME_READ`. Acknowledge/escalate require `OUTCOME_RECONCILE`, tenant
  scope, `Idempotency-Key`, a bounded reason, and an append-only operator audit record;
- state commands use compare-and-swap. `OPEN -> ACKNOWLEDGED|ESCALATED` and
  `ACKNOWLEDGED -> ESCALATED` are legal; escalation never returns to acknowledgement;
- operator state changes emit `agentmesh.runtime.integrity-incident.updated` in the same UoW but
  never mutate Task, Run, Attempt, Runtime phase, accepted output, accounting, Artifact, or Memory.

The mutable incident row is not itself the operator audit log. Add an append-only
`runtime_integrity_incident_actions` ledger before the first acknowledgement/escalation writer is
enabled. Each row stores a UUID, tenant and incident identity, action (`ACKNOWLEDGE` or `ESCALATE`),
closed `from_status`/`to_status`, actor principal ID, bounded reason, request digest, and creation
time. It contains no raw provider evidence. The generic idempotency ledger stores the command
result as a safe command-time snapshot; an exact replay returns that original snapshot even if a
later command has advanced the mutable incident row, without another action row or event. Replay
must bind the stored tenant, incident, action, request digest, transition target, and action row;
missing or inconsistent state fails closed instead of rehydrating an unrelated or newer result. A
reused key with different actor/action/reason conflicts. Neither the idempotency result nor the
event payload may contain raw provider evidence.

The incident table intentionally has no ORM-style version counter in the already-deployed A4.2a.0
schema. State changes therefore use one SQL compare-and-swap update scoped by tenant and incident:
`UPDATE ... SET status = :target, updated_at = :now WHERE status = :expected AND updated_at <=
:now`. Exactly one updated row is required. The domain, repository, ORM metadata, and migration
enforce the same closed transition set: `ACKNOWLEDGE` is only `OPEN -> ACKNOWLEDGED`, while
`ESCALATE` is only `OPEN|ACKNOWLEDGED -> ESCALATED`; audit time cannot move backwards.
Two different commands racing from the same state cannot both create audit rows. A command that
loses the CAS re-reads the row and fails closed; it does not silently claim another operator's
transition as its own. The CAS update, immutable action row, idempotency result, and Outbox event
commit in one UoW.

The public projection contains only safe identifiers, phases, digests, timestamps, state, and
bounded reasons. It never returns raw observations, provider bodies, prompts, or secret material.

## 4. Feature gates and admission

Add two explicit gates:

| Gate | Dependencies | Effect |
|---|---|---|
| `managed_runtime_reviewed_cutover` | `managed_runtime_worker`, `reviewed_execution` | First Run of a new REVIEWED Task selects managed authority |
| `managed_runtime_coordinated_cutover` | `managed_runtime_worker`, `coordinated_execution` | First scheduling transaction that persists at least one Run for a new COORDINATED Task selects managed authority |

Both gates:

- are absent from every default profile;
- are accepted only when `environment=test` and `model_provider=deterministic`;
- affect new authority cohorts only;
- do not depend on `managed_runtime_direct_cutover`;
- do not rewrite an existing Run or Task;
- may be disabled independently for rollback of new Task admission.

The Worker selects only persisted Run authority. It must not consult a cutover gate while consuming
`RunRequested`.

Rollback disables admission for new Task cohorts but keeps the managed Worker, pinned Runtime
Version, Assignment reader, and outcome/reconciliation code deployed until every previously
admitted managed reviewed/coordinated Task is terminal. Rolling back by switching a later Run in an
active Task to legacy is forbidden. Operators may stop new Task creation and drain or explicitly
fail a cohort; they may not remove its adapter while work remains.

## 5. Canonical work-item construction

Move work-item construction into an application-owned, framework-neutral component used before
both the legacy and managed branches. The exact work item is then passed to the legacy runner,
Runtime Assignment builder, and Runtime backend binding.

| Mode/role | Objective | Structured input |
|---|---|---|
| DIRECT executor | Task objective | Task input |
| REVIEWED executor revision 0 | Task objective | Task input |
| REVIEWED reviewer | Review the current candidate against the pinned acceptance contract | `candidate_output`, serialized `acceptance_criteria` |
| REVIEWED executor revision N | Task objective | Task input plus `review_context` containing revision number, previous candidate, and latest review |
| COORDINATED Subtask executor | Subtask objective | `subtask_input`, completed predecessor outputs, accepted Handoffs |
| COORDINATED Supervisor | Synthesize the coordinated result | plan version/digest and all completed Subtask outputs |

Rules:

- all maps are copied and bounded before Assignment canonicalization;
- a reviewer never receives automatic organizational Memory context;
- executor/Supervisor Memory assembly may augment the work item through the existing governed
  Memory service before the Assignment digest is computed;
- Assignment input and the input actually executed by the in-process backend must be identical;
- no framework type enters the work-item contract;
- the first Attempt persists the complete canonical, secret-free RuntimeAssignment snapshot before
  crossing dispatch;
- a replacement Attempt loads and validates that snapshot; it does not repeat Memory retrieval or
  reconstruct mutable input from current Task projections.

The existing LangGraph `_run_input` behavior becomes a compatibility test, not a second source of
input semantics.

### 5.1 Immutable Assignment snapshot

Add an execution-owned `runtime_assignment_snapshots` record keyed one-to-one by
RuntimeExecution. It contains contract name/major, Assignment ID/digest, canonical JSON payload,
and creation time. The payload is bounded by the Runtime descriptor limit, contains no credential
value, and must round-trip to the same canonical digest.

First dispatch preparation is:

1. build the role-aware work item and governed Memory context;
2. build and validate RuntimeAssignment;
3. in one transaction create/validate RuntimeExecution `PREPARED`, bind Run identity, and insert the
   immutable Assignment snapshot;
4. only then claim ownership and cross the provider dispatch boundary.

If the process dies before step 3, a replacement may rebuild because no provider dispatch exists.
If step 3 committed, every replacement loads the snapshot and skips Memory/search reconstruction.
Same execution plus different Assignment bytes is a conflict. A missing/corrupt snapshot on a
prepared or crossed managed execution parks/fails closed according to whether dispatch may have
crossed; it never silently regenerates different bytes.

## 6. Shared business outcome applier

Extract mode progression from `RunExecutionService._finalize_success/_finalize_failure` into an
application component that mutates only an already locked UoW. Both legacy and managed
finalization call it.

Inputs:

```text
Task, Run, latest Attempt
known terminal phase
validated mapping output or safe error
budget rejection computed by the caller
provider observed_at when the result was reconciled
authority cohort resolver / coordinated scheduler
```

The applier does not:

- persist Runtime observations;
- settle/release budget or quota;
- call an adapter;
- commit its UoW;
- perform Memory capture or research materialization.

Those responsibilities stay with the caller so atomic Runtime evidence remains possible.

### 6.1 Success mapping

- DIRECT executor: complete the Task or wait with candidate when budget rejects.
- REVIEWED executor: succeed Run/Attempt, retain candidate, and either queue a cohort-inheriting
  reviewer or enter `WAITING_APPROVAL`.
- REVIEWED reviewer: parse the existing `ReviewDecision`; accept, queue a cohort-inheriting bounded
  revision, or enter `WAITING_APPROVAL` using existing deadline/revision/budget policy.
- COORDINATED Subtask: complete the Subtask; schedule successors only when the Task is not under a
  Runtime reconciliation hold.
- COORDINATED Supervisor: complete the Task.

### 6.2 Non-success mapping

- failure and timeout use the existing safe failure semantics;
- provider `CANCELED` is business cancellation only when a persisted Runtime cancel intent exists;
  otherwise it is `runtime.unrequested_cancellation` failure;
- coordinated failure does not pretend an active managed sibling stopped merely because its
  database Attempt was marked canceled;
- late sibling results are retained as Runtime evidence and can finalize their own Run/Attempt, but
  cannot overwrite a terminal Task or restart scheduling.

### 6.3 Accounting and post-commit work

- ordinary known finalization settles the Attempt once and releases quota once;
- reconciliation never settles/releases again because parking already did so conservatively;
- Memory capture occurs in the same UoW only when the Task actually becomes `COMPLETED`;
- research materialization stays post-commit best effort;
- every dynamically created Run and its `RunRequested` Outbox message commit atomically with the
  preceding result.

Runtime-admission failure is distinct from budget waiting. It preserves predecessor Run evidence,
uses a stable non-budget reason, and cannot be cleared by merely increasing Task budget.

### 6.4 A4.2a.1 implementation boundary

The extraction is an application component, `BusinessOutcomeApplier`, with one caller-owned method:

```text
apply_known_terminal_in_uow(
  uow,
  task,
  run,
  attempt,
  progression_context,   # ORDINARY|DIRECT_RECONCILIATION
  phase,                 # SUCCEEDED|FAILED|CANCELED|TIMED_OUT only
  output,                # mapping only for SUCCEEDED
  safe_error,            # bounded stable reason for non-success
  budget_rejection,      # result already computed by the caller, or none
  cancel_intent_present,
  accounting_disposition,# SETTLED|RELEASED|ALREADY_CONSERVATIVE|NOT_APPLICABLE
  finalized_at,          # one control-plane UTC timestamp
  causation_id,
  accounting_transition, # optional typed before/after proof for caller-owned settlement
) -> BusinessOutcomeApplication
```

`BusinessOutcomeApplication` is an immutable summary containing the resulting Task/Run/Attempt
statuses, newly created Run identities, whether the Task became completed in this call, and whether
the caller may capture completion Memory. Its constructor validates that the summary agrees with
the supplied entities; callers must not infer completion from a stale pre-call Task snapshot.

The applier validates that Task, Run, and Attempt belong to one chain, re-reads the latest locked
Attempt, and rejects a different owner. `accounting_disposition` is a closed enum checked against
the persisted Attempt settlement source; it does not authorize settlement. The applier accepts no
`LOST` or `OUTCOME_UNKNOWN`; parking and reconciliation remain Runtime-finalizer responsibilities.
It does not load or write Runtime evidence, Usage, budget reservations, quota reservations, Inbox,
Artifact, Memory, or idempotency records, and it never opens or commits a UoW.

`PreparedAccountingTransition` is required when a UoW isolates repository reads rather than
returning the caller's already-settled identity-map objects. It is an immutable before/after proof
bound to the Task, Run, Attempt, disposition, and `finalized_at`; it is not a mutable entity
snapshot. The applier verifies the locked Attempt reservation, exact Task reserved/settled deltas,
Attempt settlement totals and source, version and policy-clock advance, and accepts the locked
accounting fields only when they equal either the proved before state or the proved after state.
`SETTLED` permits only `ACTUAL|CONSERVATIVE_ESTIMATE`, `RELEASED` requires zero Attempt totals, and
`NOT_APPLICABLE` accepts no proof. A forged or partial proof fails before any business transition,
repository save, or Outbox append. SQLAlchemy identity-map callers normally present the after state;
isolating test/in-memory UoWs may present the before state and receive only the validated accounting
field delta before the applier performs the business transition.

The accounting check is exact:

| Disposition | Required persisted state |
|---|---|
| `NOT_APPLICABLE` | `Task.budget is None` and Attempt settlement source is null |
| `SETTLED` | Task has a budget and Attempt source is `ACTUAL` or `CONSERVATIVE_ESTIMATE` after ordinary success settlement |
| `RELEASED` | Task has a budget and Attempt source is `RELEASED` after ordinary non-success |
| `ALREADY_CONSERVATIVE` | Task has a budget and the parked Attempt source is `CONSERVATIVE_ESTIMATE`; this call performs no second settlement |

No-budget ordinary or reconciliation paths use `NOT_APPLICABLE`; null settlement source is never
silently treated as settled for a budgeted Task.

Save ownership is exclusive: after all preconditions and the complete transition plan validate,
the applier **must** save every changed Task/Run/Attempt/Subtask, add every continuation Run, and
append each continuation `RunRequested` Outbox message. The caller must not save those business rows
or append those continuation messages again after a successful call. The caller remains the sole
owner of Runtime evidence, Usage/accounting/quota records, Memory, Inbox, and commit. Unsupported or
invalid combinations fail before the applier calls any repository or Outbox method.

Legacy coordinated sibling shutdown preserves that boundary with a caller-prepared accounting
handshake. After classification and under the same UoW, the caller releases budget and quota for
each active sibling Attempt that the closed outcome will stop, but does not save the mutated
Task/Run/Attempt rows. The applier re-reads the fixed sibling set under lock before any business
mutation and requires every budgeted active sibling Attempt to have settlement source `RELEASED`
(`null` for a no-budget Task). It then owns the Subtask/Run/Attempt cancellation mutations and all
business-row saves. A missing sibling release is an invalid accounting pre-state; the transaction
rolls back the caller-owned quota/accounting work and the applier performs no business save or
Outbox append. The applier never invokes `BudgetController` or `QuotaController` itself.

The caller owns the atomic ordering. Before accounting it first classifies the locked aggregate as
`ACTIVE_BUSINESS_OUTCOME`, `CANCELED_TASK_RUNTIME_ONLY`, `COORDINATED_BARRIER_OUTCOME`, or invalid:

1. lock and revalidate Task, Run, latest Attempt, and (for managed authority) RuntimeExecution;
2. persist/validate Runtime evidence when present;
3. classify the business pre-state before any Usage, budget, quota, business-row, or Outbox write;
4. for `ACTIVE_BUSINESS_OUTCOME`, persist Usage and settle or release Attempt budget exactly once,
   release quota exactly once, then invoke the applier;
5. for `CANCELED_TASK_RUNTIME_ONLY`, skip Usage/accounting/quota, the applier, continuation,
   completion Memory, and research entirely and use the dedicated §9.2 convergence path;
6. for `COORDINATED_BARRIER_OUTCOME`, settle only the target Attempt, apply only its local
   Run/Subtask result, and invoke the mandatory §8 barrier in the same UoW;
7. capture Memory only when the returned summary says this call completed the Task;
8. append Inbox and commit once.

Legacy success maps to `SUCCEEDED`; legacy execution failure maps to `FAILED`. Managed known-terminal
observations map one-for-one after the terminal contract and cancel-intent checks. Managed
`LOST|OUTCOME_UNKNOWN` never enter the applier. A4.2a.1 DIRECT reconciliation calls the same applier
with `progression_context=DIRECT_RECONCILIATION`; it uses the dedicated reconcile domain transitions
and skips accounting/quota because parking already settled them conservatively. REVIEWED and
COORDINATED reconciliation contexts are added only with A4.2b/A4.2c.

`finalized_at` is captured by the control plane once per transaction. It controls review-deadline
and other policy decisions. Every domain transition used here gains an explicit `at=` parameter;
the applier may not call a transition that internally samples a second `utc_now()`. Provider
`observed_at` remains evidence only and must never extend a deadline, move a policy clock backwards,
or be copied into Task/Run/Attempt update timestamps. This clarifies the earlier high-level input
list: a reconciled provider timestamp may be retained with Runtime evidence, but it is not the
business-policy clock.

### 6.5 Closed progression and activation table

The discriminator is `(progression_context, runtime_authority, execution_mode, run_role,
subtask_binding,
Task/Run/Attempt pre-state, active-drain class, phase, accounting_disposition)`. The following table
describes active business outcomes; exact allowed pre-states are `RUNNING` for Task/Run/Attempt,
`REVIEWING` for an active reviewer Task, and the explicit pause exception below. The one A4.2a.1
reconciliation row requires managed DIRECT authority plus exact
`RECONCILIATION_REQUIRED/RECONCILIATION_REQUIRED/OUTCOME_UNKNOWN` Task/Run/Attempt state,
`DIRECT_RECONCILIATION`, no active drain, and `ALREADY_CONSERVATIVE` or `NOT_APPLICABLE`. It invokes
`reconcile_runtime_succeeded/failed/canceled(..., at=finalized_at)`, uses `finalized_at` rather than
provider time for budget-deadline policy, saves through the applier, schedules no continuation, and
returns the action/reason needed by the caller's immutable resolution audit. Every other
reconciliation or terminal-Task combination uses its dedicated milestone path or fails before any
repository save or Outbox append.

| Authority / mode / binding | `SUCCEEDED` | `FAILED|TIMED_OUT` | `CANCELED` | Activation |
|---|---|---|---|---|
| legacy / DIRECT / EXECUTOR | succeed Run/Attempt; complete Task, or retain candidate and wait on post-settlement budget rejection | fail Run/Attempt/Task with the safe reason | preserve existing legacy user-cancel behavior | A4.2a.1 parity |
| managed / DIRECT / EXECUTOR | same business progression as legacy after Runtime evidence | fail active chain with safe reason | cancel only with persisted intent; otherwise fail as `runtime.unrequested_cancellation` | A4.2a.1 |
| legacy / REVIEWED / EXECUTOR | succeed Run/Attempt, retain candidate, queue one reviewer or wait on budget | fail active chain | preserve legacy cancel behavior | A4.2a.1 parity |
| legacy / REVIEWED / REVIEWER | succeed Run/Attempt, parse `ReviewDecision`, accept, queue bounded revision, or wait for approval | fail active chain | preserve legacy cancel behavior | A4.2a.1 parity |
| managed / REVIEWED / EXECUTOR or REVIEWER | same reviewed rules through cohort continuations | fail/cancel through reviewed reconciliation rules | persisted-intent rule | A4.2b only; rejected in A4.2a.1 |
| legacy / COORDINATED / EXECUTOR with Subtask | succeed Run/Attempt/Subtask and invoke legacy scheduler | preserve current immediate legacy coordination failure | preserve legacy cancel behavior | A4.2a.1 parity |
| legacy / COORDINATED / SUPERVISOR without Subtask | succeed Run/Attempt and complete Task | fail active chain | preserve legacy cancel behavior | A4.2a.1 parity |
| managed / COORDINATED / EXECUTOR with Subtask | apply only local Run/Attempt/Subtask terminal result, then mandatory §8 barrier | same; barrier records `FAILED` target | same; barrier records `CANCELED` only with intent | A4.2c only; rejected earlier |
| managed / COORDINATED / SUPERVISOR without Subtask | complete/fail only through the reviewed-like single-Run hold | same | persisted-intent rule | A4.2c only; rejected earlier |

For managed `CANCELED`, `cancel_intent_present` is derived from the locked persisted Runtime
lifecycle intent, not from provider text. Without that intent, provider cancellation is a failure
with the stable safe reason above. If Task, Run, and Attempt are already `CANCELED`, all four known
terminal conclusions are `CANCELED_TASK_RUNTIME_ONLY`: they retain those states, perform zero
accounting/quota mutation, and never call the ordinary applier, exactly as §9.2 requires. Legacy
cancellation behavior remains unchanged and does not fabricate a Runtime intent.

The finalizer derives this flag by calling the tenant-scoped Runtime repository's
`find_cancel_intent(...)` while the RuntimeExecution and business chain are locked in the same UoW.
`ManagedRuntimeAuthoritativeResult` deliberately does not carry a cancel-intent flag: adapter output
is provider evidence and cannot authorize a business cancellation. A result-side flag, provider
message, or observed Runtime phase is never an acceptable substitute for the persisted intent.

Terminal `RuntimeObservation.usage` is also deliberately not converted into `UsageRecord`. In
A4.2, a conforming terminal observation has empty usage; attributable usage must already have been
reported through the Attempt-bound usage channel with provider/model/trace and idempotency lineage.
A usage-bearing terminal observation fails the terminal contract and is parked as conflicting
`OUTCOME_UNKNOWN` evidence. A valid terminal observation with no prior attributable records settles
the reservation conservatively under the existing budget policy. This rule prevents an untyped
provider mapping from becoming trusted accounting data.

Pause-request handling is not silently folded into ordinary domain methods. The legacy caller
preserves its existing pause acknowledgement branch before invoking the applier. A managed known
terminal result wins over an outstanding pause request because the provider has already terminated;
the domain adds explicit `finalize_managed_after_pause_request(..., at=finalized_at)` transitions on
Task, Run, and Attempt. They accept only the exact aligned `PAUSE_REQUESTED/PAUSE_REQUESTED/RUNNING`
pre-state, clear pause projections, and produce the same terminal business state as the phase table.
They cannot create a resumable Run. Lifecycle receipt handling records the pause outcome separately.
This is an intentional managed/legacy parity exception and must be reported as such.

For managed coordinated work the ordinary applier returns a closed `CoordinationBarrierIntent`
after applying only the target Run/Attempt/Subtask result. It cannot save Task terminal state or
schedule a continuation. The caller must invoke the §8 barrier under the fixed aggregate lock set in
the same UoW; only that barrier may set/complete a drain, create sibling cancel intents, change Task
status, or call the cohort-aware scheduler. If the Task became terminal because another branch
finished first, the barrier allows only the late sibling's local convergence. A database-only
sibling cancel is not proof of provider cancellation. Before A4.2c provides the drain repository,
domain states, and barrier, every managed COORDINATED combination is rejected before mutation.

### 6.6 Continuation creation dependency

The applier never calls `TaskRun.request` directly for reviewer, revision, Subtask, or Supervisor
continuations. It depends on `AuthorityCohortResolver.create_continuation_in_uow(...)`, which returns
a fully bound immutable Run for the Task cohort. The resolver:

- inherits `runtime_authority`, `runtime_version_id`, and `comparison_mode` from the frozen cohort;
- rejects mixed authority/version evidence across existing Task Runs;
- verifies the selected Runtime Version under lock, allowing `DEPRECATED` only for an existing
  managed cohort and rejecting missing or `REVOKED` versions;
- creates a distinct stable execution intent for every managed continuation and persists it once;
- never consults a cutover gate for an existing cohort;
- adds neither Outbox nor commit. The applier persists the returned Run and its `RunRequested`
  message atomically with the predecessor outcome.

`TaskApplicationService`, `RunExecutionService`, `CoordinatedScheduler`, and
`TaskResolutionService` receive the same resolver/factory through constructor injection. They must
use it at every site enumerated in §3.1: initial admission, reviewer/revision continuation,
Subtask/Supervisor scheduling, human replacement/resume, and safe never-dispatched recreation. A CI
architecture test scans those local services and rejects a raw `TaskRun.request(...)` call outside
the resolver. Federated/A2A and isolated showcase fixture construction remain the explicit bounded
exceptions. A4.2a.1 tests exercise inheritance with synthetic managed cohorts while
reviewed/coordinated admission gates remain absent and disabled. No production request can select a
new managed reviewed/coordinated cohort until A4.2b/A4.2c gates are implemented.

Legacy coordinated scheduling used by the applier is a two-phase operation. `plan(...)` locks and
validates the Subtask DAG, agent/version selection, cohort, budget decision, and the exact set of
continuation Runs without changing Task/Subtask/Run state or writing Outbox. `apply(plan, ...)`
revalidates the plan token against the same aggregate state, performs the queued/ready transitions,
persists each planned Run, and appends exactly one causation-bound `RunRequested` message per Run.
The applier must obtain a complete plan before it completes the predecessor Subtask. If planning
fails, no business entity, repository save, or Outbox entry changes. Direct calls to the existing
one-shot `schedule(...)` remain a compatibility wrapper outside the applier and internally execute
the same plan/apply pair.

For A4.2a.1 legacy parity, an executor success without budget rejection completes its Subtask and
applies the scheduler plan. Executor failure or timeout fails the Subtask and Task, while executor
cancellation cancels the Task; all three terminal-stop paths cancel every nonterminal sibling.
Success with a post-settlement budget rejection completes the target Subtask, moves the Task to
`WAITING_APPROVAL`, and cancels every nonterminal sibling. A Supervisor success completes the Task,
success with budget rejection retains the candidate and waits for approval, failure/timeout fails
the Task through the active Supervisor Run, and cancellation cancels the Task. Sibling shutdown is
idempotent over already-terminal Subtasks/Runs/Attempts and excludes the target Run.

### 6.7 Extraction and parity acceptance

Implementation proceeds without a flag day:

1. characterize the current legacy DIRECT, REVIEWED, and COORDINATED matrix, including budget,
   review deadline/revision, pause, cancellation, sibling failure, continuation Outbox, and Memory;
2. introduce the applier and continuation resolver behind the existing callers;
3. switch legacy finalization to the applier with byte-for-byte equivalent safe outputs and the same
   number of Task/Run/Attempt/Outbox mutations;
4. switch managed known-terminal DIRECT finalization to the same applier; keep unknown parking
   outside it;
5. prove that both authorities given equivalent inputs end in equivalent business state.

Required tests include every row in the closed table; pre/post-settlement budget rejection; review
accept/revise/deadline/limit; continuation cohort inheritance; exact one continuation message;
unrequested cancellation; managed terminal result during pause request; coordinated late sibling;
Memory only on the transition to `COMPLETED`; failure injection before continuation Outbox and before
commit; unchanged Inbox idempotency; and real PostgreSQL rollback of predecessor state plus any
created continuation. PostgreSQL acceptance also covers exact replay and two concurrent deliveries
of the same known-terminal Inbox item; exact counts for Usage, Task/Attempt settlement totals and
source, quota release, Memory, and continuation Outbox; zero mutation for every invalid pre-state;
all four canceled-Task Runtime-only conclusions with zero accounting; the managed
`PAUSE_REQUESTED` exact post-state; and, in A4.2c, every drain target crossed with late
`SUCCEEDED|FAILED|CANCELED|TIMED_OUT` siblings. Existing legacy tests are regression requirements,
not permission to preserve duplicated finalization code.

### 6.8 A4.2a.1 DIRECT convergence verification matrix

The following matrix is the executable acceptance contract for the A4.2a.1 caller cutover. A test
name or a green legacy suite is not evidence unless the assertions cover the listed business state,
Runtime evidence, accounting, quota, Memory, Outbox, and transaction boundary. Unit tests use a
Runtime-aware UoW spy; PostgreSQL tests repeat the concurrency and rollback rows against the real
repositories.

| Pre-state and trusted input | Required result | Forbidden effects |
| --- | --- | --- |
| active managed DIRECT executor, `SUCCEEDED`, empty usage and mapping output | Run/Attempt succeed; Task completes unless post-settlement budget policy retains the candidate; evidence and Inbox commit once | no continuation; no second settlement or quota release on replay |
| active managed DIRECT executor, `FAILED` or `TIMED_OUT` | Run/Attempt/Task fail with `runtime.failed` or `runtime.timed_out`; reservation is released | no output, Memory, research materialization, or continuation |
| active managed DIRECT executor, `CANCELED`, persisted matching lifecycle cancel intent | Run/Attempt/Task cancel and reservation is released | no success/failure rewrite and no untrusted adapter intent |
| active managed DIRECT executor, `CANCELED`, no matching persisted intent | fail with `runtime.unrequested_cancellation`; reservation is released | never report user cancellation |
| active managed DIRECT executor, `LOST` or `OUTCOME_UNKNOWN` | park Task/Run/Attempt for reconciliation at one control-plane timestamp; settle conservatively; emit exactly one reconciliation-required event | no known-terminal applier call, completion, Memory, research, or redispatch |
| any known terminal observation with usage, unresolved action/wait request, invalid success output, success plus error, or crossed assignment metadata | persist bounded conflict/synthetic unknown evidence and park as unknown | never apply the claimed terminal business result or deserialize arbitrary usage into accounting |
| exact managed DIRECT chain in `PAUSE_REQUESTED` | apply the same terminal conclusion allowed for the active chain, preserving the documented pause race semantics | no return to `RUNNING` and no extra pause/resume command |
| exact Task/Run/Attempt canceled chain plus matching persisted cancel intent, any known terminal phase | append Runtime evidence only; quarantine a late success output | zero Task/Run/Attempt, budget, quota, Memory, research, Artifact, or business Outbox mutation |
| same canceled runtime-only chain, `LOST` or `OUTCOME_UNKNOWN` | append Runtime evidence and exactly one reconciliation-required event | all business and accounting writes remain zero |
| canceled chain without intent, partial/mismatched chain, wrong current Run, wrong owner/fence, REVIEWED/COORDINATED mode, reviewer/supervisor role, or Subtask-bound Run | reject before authoritative mutation | zero Inbox, evidence, business, accounting, quota, Memory, research, and Outbox writes |

Ordinary finalization must additionally prove pre-settlement rejection, post-settlement budget
rejection, budgetless success, failure release, and exact `UsageRecord`/Task/Attempt settlement
totals and source. Memory is captured exactly once and only when this transaction transitions the
Task to `COMPLETED`; research materialization is attempted only for that same transition and remains
non-authoritative.

Reconciliation tests start from a genuinely parked managed DIRECT chain and exercise
`SUCCEEDED|FAILED|CANCELED|TIMED_OUT`. The conclusion uses the reconciliation control-plane clock,
not provider `observed_at`; performs no second accounting or quota release; writes one stable
`TaskResolution` and one outcome-reconciled event with stable causation; and is idempotent under
exact replay. A canceled runtime-only reconciliation repeats all four known conclusions. A late
success following an already recorded exact conflict creates deterministic quarantine evidence once
without mutating business state.

Failure injection is required immediately before continuation/event Outbox insertion and before
commit. The real-PostgreSQL suite must prove full rollback, exact replay, and one winner for two
concurrent deliveries of the same Inbox item. After rollback, predecessor Task/Run/Attempt,
RuntimeExecution/evidence/Inbox, budget/quota, Memory, and Outbox rows must be byte-for-byte
equivalent to their pre-transaction projections.

## 7. Reviewed cutover state machine

Reviewed execution has one active business Run at a time, so its reconciliation hold can reuse
Task `RECONCILIATION_REQUIRED` with `current_run_id`.

### 7.1 Ordinary flow

```text
managed executor Run
  -> candidate
  -> managed reviewer Run (same cohort)
  -> accepted -> COMPLETED
     rejected + limits available -> managed revision Run (same cohort)
     rejected + limit/deadline/budget -> WAITING_APPROVAL
```

### 7.2 Unknown outcome

- Executor or Reviewer Runtime `LOST/OUTCOME_UNKNOWN` atomically parks Runtime, Task, Run, and
  Attempt and emits the existing reconciliation-required event.
- No reviewer or revision Run is created while parked.
- The privileged reconciliation command accepts the same strict evidence contract as DIRECT.
- Confirmed executor success enters the ordinary candidate/reviewer transition through the shared
  outcome applier.
- Confirmed reviewer success applies the normal review decision and bounded revision policy.
- Confirmed non-success fails/cancels according to the persisted cancel-intent rule.
- Idempotency, competing conclusions, rollback, stale fencing, and no-redispatch guarantees are
  unchanged.

The reconciliation resolution action remains the Runtime terminal action; details record the Task
mode, Run role, revision, and any newly queued Run ID.

### 7.3 Reviewed admission and cohort ownership

`managed_runtime_reviewed_cutover` is the only policy input that may select managed authority for
the first executor Run of a new REVIEWED Task. `request_run` evaluates it while holding the Task and
the complete existing Run set. It requires an empty Run set, the test/deterministic startup guard,
and a coherent built-in Runtime Version; it then persists the executor Run, its generated execution
intent identity, the Task queue transition, and `RunRequested` in one transaction. It does not
prepare or dispatch a RuntimeExecution.

Every reviewer and revision Run is created through `AuthorityCohortResolver` from the locked parent
and full Task cohort. The continuation inherits `runtime_authority=managed`, the exact pinned
Runtime Version, and `comparison_mode=off`, and receives a fresh Run ID and execution-intent ID.
Continuation creation never reads the admission gate. Any mixed authority/version/comparison cohort
fails before the parent result, candidate, budget, or Outbox is mutated. Turning the gate off affects
only Tasks with no Run; an existing managed reviewed Task remains executable and reconcilable.

### 7.4 Role-specific ordinary finalization

The managed worker validates the persisted mode, role, current Run, Attempt fence, Runtime owner,
Assignment identity, and terminal observation before accounting. It then invokes the shared
business outcome applier in the same UoW as Runtime evidence, Inbox, accounting, quota, Memory, and
continuation Outbox.

| Current role and conclusion | Atomic business transition |
| --- | --- |
| executor `SUCCEEDED` | settle the Attempt; persist candidate output; create exactly one reviewer Run at the same revision and one causation-bound `RunRequested`, unless budget policy moves the Task to `WAITING_APPROVAL` |
| reviewer `SUCCEEDED`, accepted decision | settle the Attempt; persist the decision; complete the Task with the existing candidate; capture completion Memory once |
| reviewer `SUCCEEDED`, rejected, revision available | settle the Attempt; persist the decision; increment the Task revision; create exactly one executor Run at revision N+1 and one causation-bound `RunRequested` |
| reviewer `SUCCEEDED`, rejected, deadline/limit/budget reached | settle the Attempt; persist the decision and candidate; move to `WAITING_APPROVAL`; create no continuation |
| either role `FAILED` or `TIMED_OUT` | release the reservation, fail current Run/Attempt/Task with the stable Runtime reason, and create no continuation |
| either role `CANCELED` with matching persisted intent | release the reservation and cancel current Run/Attempt/Task |
| either role `CANCELED` without matching persisted intent | release the reservation and fail with `runtime.unrequested_cancellation` |
| either role `LOST` or `OUTCOME_UNKNOWN` | conservatively settle and park the exact current chain; create no reviewer/revision Run |

The review decision is application data, not Runtime protocol data. A structurally valid Runtime
success whose output cannot produce the exact pinned `ReviewDecision` is recorded as Runtime
`SUCCEEDED` evidence but fails the business Run/Attempt/Task with the bounded stable reason
`review.invalid_decision`; it is consumed once and is never redispatched or converted into an
unknown provider outcome. Raw validation text is not persisted or emitted.

That split also fixes accounting semantics: the provider did execute successfully, so a budgeted
Attempt uses the ordinary successful/actual settlement proof (empty usage in A4.2b) and releases
quota once even though its business Attempt status becomes `FAILED`. It must not turn the provider
success into a zero-cost release. The outcome applier therefore keeps the validated Runtime phase
used for accounting separate from the derived business phase used for Run/Attempt/Task mutation.
For an unbudgeted Task the disposition remains `NOT_APPLICABLE`.

Reviewer work items are built only by `CanonicalWorkItemBuilder` from the locked candidate and
serialized acceptance criteria and never receive organizational Memory. Revision executor work
items include the locked previous candidate, latest decision, and revision number; governed Memory
augmentation, when enabled, occurs once before the immutable Assignment snapshot is written. A
replacement Attempt must reload that snapshot byte-for-byte.

### 7.5 Reviewed pause and cancellation races

A4.2b supports pause only while the reviewed Task's current executor Run is in the existing exact
`RUNNING/RUNNING` pair. Reviewer Runs in `REVIEWING` reject pause because the Task domain has no
review-pause state. A terminal executor result racing an already persisted `PAUSE_REQUESTED` pair
is authoritative: the pause request is cleared and the role-specific §7.4 transition runs at the
same clock. Thus success may atomically enter `REVIEWING` and create the reviewer continuation;
failure/timeout/cancellation becomes terminal. It must not complete a REVIEWED Task using the
DIRECT-only aligned-pause transition, remain paused after consuming a terminal result, or emit an
extra pause/resume command.

User cancellation locks the Task, current Run/latest Attempt, RuntimeExecution, and lifecycle rows
in the single-active lock order. For a crossed managed execution it creates/reuses the deterministic
`CANCEL` lifecycle intent before marking the business chain canceled; for absent or `PREPARED`
execution it uses the provider-free abort contract. Budget and quota release exactly once in that
transaction. A late terminal observation is accepted only by the canceled-chain Runtime-only path;
late success output is quarantined and cannot recreate a reviewer/revision continuation. A queued
continuation canceled before acquisition is consumed without adapter invocation.

#### 7.5.1 Single-active cancellation command contract

`TaskApplicationService.cancel_task` is the single transaction owner for managed DIRECT and
REVIEWED cancellation. The command takes one control-plane timestamp and derives a bounded Runtime
cancel deadline from the configured cancellation window. The window is a constructor dependency
with a production default; it must be positive, and the derived deadline is persisted once rather
than recomputed by lifecycle retries. API callers do not supply provider deadlines.

While holding the Task, current Run, latest Attempt, and the Run-bound RuntimeExecution in the
single-active lock order, the command classifies exactly one of these states:

| Persisted state | Cancellation action in the same UoW |
| --- | --- |
| current Run is queued and has no Attempt/RuntimeExecution | cancel Task and Run; create no Runtime or lifecycle row |
| current running Attempt has no RuntimeExecution | cancel Task/Run/Attempt and release budget/quota once; create no lifecycle row |
| exact owned RuntimeExecution is `PREPARED` | apply the provider-free `runtime.dispatch_aborted` transition, cancel Task/Run/Attempt, release budget/quota once, and create no lifecycle row or adapter work |
| exact owned RuntimeExecution is crossed and nonterminal | create/reuse `runtime-cancel:{execution_id}:v1`, persist its one lifecycle Outbox wake-up, move Runtime to `CANCEL_REQUESTED`, then cancel Task/Run/Attempt and release budget/quota once |
| exact RuntimeExecution is already terminal but its business result is not consumed | persist the same stable cancel intent as `REJECTED` without provider work, then let user cancellation win; the later result is Runtime-only evidence |

`PREPARED` abort requires the Runtime owner Attempt and fencing token to equal the locked current
Attempt. A missing or mismatched binding, multiple active/unresolved executions, an unsupported
mode/role/Subtask binding, or a partially terminal business chain fails before any mutation. The
provider-free transition is an explicit RuntimeExecution domain method; ordinary provider
observation validation is not weakened. Its bounded reason is emitted as internal control-plane
audit/Outbox evidence and is never represented as a provider cancellation receipt.

Lifecycle intent construction has one in-UoW implementation reused by the public Runtime command
wrapper and this Task command. Same operation/same bytes is a no-op; changed operation identity,
deadline, or digest conflicts. The generic lifecycle consumer remains the only adapter caller.
The Task command never calls `request_cancel`, inspect, or any provider method while locks are held.

Exact cancellation replay returns the already-canceled aggregate without another accounting/quota
release, lifecycle row, Outbox message, or Runtime mutation. Delivery of a queued or provider-free
aborted Run is consumed through Inbox without creating an Attempt, Assignment, handle, or adapter
call. For a crossed execution, a late `SUCCEEDED|FAILED|CANCELED|TIMED_OUT` observation with the
persisted intent updates Runtime evidence only and preserves the canceled REVIEWED Task, candidate,
review projection, Run, Attempt, accounting, quota, Memory, Artifact, and continuation Outbox.
Late success output is quarantined. `LOST|OUTCOME_UNKNOWN` parks only Runtime and emits the existing
reconciliation-required event; B5 owns its privileged convergence.

### 7.6 Reviewed reconciliation writer

Reviewed reconciliation adds two explicit progression contexts rather than weakening DIRECT
validation: executor reconciliation and reviewer reconciliation. Both require the exact parked
Task/current Run/Attempt/Runtime chain, persisted managed cohort, owner/fence, empty terminal usage,
and operator evidence/idempotency contract. They perform no second accounting or quota release.

- confirmed executor success enters the same candidate/reviewer transition as §7.4;
- confirmed reviewer success parses and applies the decision through the same decision adapter;
- confirmed failure, timeout, or cancellation uses the same persisted-intent rule;
- a confirmed conclusion is finalized at the reconciliation control-plane clock, never provider
  `observed_at`;
- `TaskResolution`, Runtime evidence, one outcome-reconciled event, any continuation Run and its one
  `RunRequested`, Inbox/idempotency, and business state commit together;
- exact replay returns the prior result, while a competing digest or conclusion conflicts with zero
  mutation.

The summary records mode, role, revision, candidate digest, decision digest when present, and new
Run ID when present. It never embeds candidate/review payloads in audit or Outbox metadata.

### 7.7 A4.2b executable acceptance

Unit and real-PostgreSQL tests cover executor success/failure/timeout/requested and unrequested
cancellation/unknown; reviewer accept/reject/deadline/limit/budget/invalid-decision/unknown; every
continuation's cohort inheritance and exact one Outbox message; reviewer Memory exclusion and
completion Memory exactly once; executor pause race and reviewer pause rejection; cancellation
before prepare, at `PREPARED`, and after `DISPATCHING`; late known conclusions after cancellation;
and executor/reviewer reconciliation for all four known terminal phases.

PostgreSQL tests additionally inject failure before continuation Outbox and before commit, replay
the same Inbox and reconciliation idempotency key, race two deliveries of the same observation, and
assert one continuation winner. Every rollback assertion includes Task/Run/Attempt, candidate and
review projections, RuntimeExecution/evidence/Inbox, budget/quota, Memory, TaskResolution, and
Outbox. The gate remains disabled on the server until this matrix and the legacy-versus-managed
reviewed parity fixtures are green.

### 7.8 A4.2b implementation slices and ownership

A4.2b requires no schema migration: reviewed Task/Run state, Runtime snapshots, lifecycle rows,
evidence, Inbox/Outbox, accounting, Memory, and TaskResolution already exist. If implementation
appears to require a new status or nullable authority field, stop and amend this design before
writing a migration.

Implementation lands in independently reviewable slices:

1. **B1 — gate and admission:** add the feature enum/spec, test-only deterministic startup guard,
   mode-aware initial cohort selection, and gate-off/existing-cohort tests. No Worker behavior
   changes in this slice.
2. **B2 — reviewed finalizer activation:** replace the A4.2a.1 managed-mode rejection with a closed
   DIRECT/REVIEWED dispatcher; reuse terminal validation, accounting preparation, the business
   outcome applier, canonical work items, snapshots, and Inbox handling. Add no second reviewed
   transition implementation.
3. **B3 — continuation and pause races:** prove reviewer/revision cohort inheritance, exact
   causation-bound continuation Outbox, invalid-decision consumption, aligned executor pause, and
   queued-continuation cancellation.
4. **B4 — managed reviewed cancellation:** route `cancel_task` through one application command that
   owns business cancellation plus lifecycle intent/provider-free abort. The generic lifecycle
   consumer remains the only adapter caller.
5. **B5 — reviewed reconciliation:** add explicit executor/reviewer reconciliation contexts and
   mode-aware validation/application; reuse the ordinary continuation planner and transaction-owned
   persistence.
6. **B6 — PostgreSQL and parity qualification:** execute §7.7, compare the frozen legacy fixtures,
   and only then make the CI admission fixture enable the gate. Production/server configuration
   remains off.

The expected primary code owners are `features.py`, bootstrap configuration validation,
`authority_cohorts.py`, `services.py`, `business_outcomes.py`, `runtime_reconciliation.py`, and their
unit/integration tests. Runtime SDK DTOs, adapter protocol, PostgreSQL models, and Alembic revisions
are not expected to change. Each slice must pass architecture import checks so reviewed policy does
not leak into the public Runtime SDK or adapter.

## 8. Coordinated reconciliation model

Coordinated execution may have multiple active Subtask Runs. A single `current_run_id` cannot model
all unresolved Runtime outcomes. Add `RECONCILIATION_REQUIRED` to `SubtaskStatus` and its database
constraint in an expand/read-compatible migration before any writer uses it.

Add one active `CoordinationRuntimeDrain` per coordinated Task. It records the immutable drain ID,
Task, triggering Run, target Task outcome (`RUNNING`, `WAITING_APPROVAL`, `FAILED`, or `CANCELED`),
bounded reason, status (`DRAINING` or `COMPLETE`), and timestamps. `RUNNING` means the hold may
resume scheduling after uncertainty closes. The first terminal target wins; a later provider result
cannot change `FAILED` to `CANCELED` or vice versa. A nonterminal `RUNNING`/`WAITING_APPROVAL`
target may be upgraded by the first known failure or user cancellation while the drain row is
locked. This row, not `Task.error`, preserves the convergence intent.

Drain targets have one closed behavioral classification:

- `RUNNING` is the only resumable hold. It aborts sibling work that has not crossed dispatch but
  does not request cancellation of already crossed siblings; after all uncertainty closes, normal
  DAG scheduling may resume.
- `WAITING_APPROVAL`, `FAILED`, and `CANCELED` are **stopping targets**. Each stops admission and
  dispatch of not-yet-crossed sibling work and requests cancellation of every already crossed
  nonterminal sibling before the target is applied to the Task.

`WAITING_APPROVAL` is nonterminal as a Task status but is a stopping drain target. In particular, a
budget rejection may not allow already admitted sibling work to continue spending merely because
the eventual Task status can later be resumed.

### 8.1 Parking one Subtask

When a managed Subtask Runtime becomes `LOST/OUTCOME_UNKNOWN`, one transaction:

1. locks Task, all relevant Subtasks/Runs/latest Attempts in deterministic UUID order, then the
   target Runtime execution;
2. revalidates target ownership/fence and the coordinated authority cohort;
3. records the immutable Runtime observation;
4. settles the target Attempt conservatively and releases only its quota reservation;
5. marks target Runtime/Run/Attempt and target Subtask `RECONCILIATION_REQUIRED`/
   `OUTCOME_UNKNOWN`;
6. creates/reuses a `RUNNING` CoordinationRuntimeDrain and changes Task from `RUNNING` to
   `RECONCILIATION_REQUIRED` with no single active Run; a second parked sibling reuses the drain,
   leaves the generic Task error unchanged, and records its reason on that Subtask/Run;
7. aborts/releases every sibling Run that is provably not crossed, including `QUEUED` Runs and
   current `RUNNING` Attempts whose RuntimeExecution is absent or `PREPARED`; their Subtasks return
   to `READY` and may be recreated after convergence;
8. leaves already crossed active sibling Runs owned and running; it neither redispatches nor
   falsely marks them canceled;
9. emits one reconciliation-required Outbox event and commits Inbox dedupe atomically.

No successor or Supervisor is scheduled while the Task is held.

### 8.2 Active siblings while held

- An already active sibling may record a known terminal result while Task status is
  `RECONCILIATION_REQUIRED`.
- Its Run/Attempt/Subtask becomes terminal, accounting settles normally, and no successor is
  scheduled.
- It may also park independently, producing another Subtask reconciliation item.
- Replacement processing for a crossed/expired sibling uses the same no-redispatch rule.
- A newly delivered message for a safe, never-dispatched queued sibling is consumed as canceled;
  it cannot start while the Task is held.

### 8.3 Convergence barrier

After every active-sibling terminal result and every operator reconciliation, a transaction-local
barrier evaluates the whole coordinated Task:

1. lock/revalidate the active drain; if none exists, create the mode-appropriate drain before doing
   anything to siblings;
2. inspect persisted Subtask results. If the target is still nonterminal and a known failure or
   unexpected cancellation exists, atomically set the first terminal target/reason;
3. when a stopping drain target (`WAITING_APPROVAL`, `FAILED`, or `CANCELED`) exists, ensure exactly
   one stable `CANCEL` lifecycle intent for every crossed active sibling and safely abort or release
   every not-yet-crossed sibling before testing whether active work remains;
4. if any crossed sibling remains active, keep the stopping drain; its known terminal or
   unknown-cancel result will re-enter this barrier;
5. if any Run/Subtask remains reconciliation-required, keep the hold for operator evidence;
6. otherwise atomically mark the drain complete and apply its target: fail, cancel, enter budget
   approval, or restore Task `RUNNING` and invoke the ordinary DAG scheduler;
7. a resumed scheduler recreates safe canceled queued work, schedules newly unblocked successors,
   or queues the managed Supervisor using the original authority cohort.

The barrier is idempotent. It never changes a terminal Task and never creates more than one Run for
the same ready Subtask under concurrent reconciliation commands. Concurrent failures serialize on
the drain row, retain the first terminal cause, and create at most one cancellation operation for
each target RuntimeExecution.

Known coordinated failure, user cancellation, and budget rejection all enter this drain protocol
instead of immediately marking active managed sibling Attempts canceled. Their drain targets are
`FAILED`, `CANCELED`, and `WAITING_APPROVAL` respectively. A late sibling result settles only that
Attempt and updates only its Run/Subtask plus the barrier; it cannot overwrite the drain target or
schedule new work.

### 8.4 Pre-dispatch sibling abort and dispatch-boundary race

`RunStatus.RUNNING` is not evidence that provider dispatch crossed. A Worker can have committed a
current Attempt while the RuntimeExecution is still absent or `PREPARED`. The coordinated aggregate
therefore classifies sibling work under locks as follows:

| Persisted state | Boundary classification | Drain action |
|---|---|---|
| Run `QUEUED`, no current running Attempt, no crossed RuntimeExecution | never dispatched | cancel the old Run, clear its Subtask binding, and return the Subtask to `READY` |
| Run `RUNNING`, current Attempt `RUNNING`, RuntimeExecution absent | never dispatched | atomically fence/cancel the Attempt and Run, release budget/quota reservations once, clear the Subtask binding, and return it to `READY` |
| Run `RUNNING`, current Attempt `RUNNING`, RuntimeExecution `PREPARED` | never dispatched | atomically apply a provider-free `runtime.dispatch_aborted` terminal control-plane transition to the RuntimeExecution, fence/cancel Attempt and Run, release reservations once, clear the Subtask binding, and return it to `READY` |
| RuntimeExecution `DISPATCHING`, `ACCEPTED`, or another nonterminal post-boundary phase | crossed | do not mark provider work stopped; create/reuse its stable lifecycle `CANCEL` intent for a stopping drain, or leave it active for a resumable `RUNNING` hold |
| RuntimeExecution terminal | terminal evidence | apply/retain the fenced terminal result and re-enter the barrier; do not send cancellation |

Returning a Subtask to `READY` preserves it only as undispatched work. A completed stopping drain
with target `FAILED` or `CANCELED` cancels remaining `READY`/`BLOCKED` Subtasks when it applies the
Task target. A `WAITING_APPROVAL` drain leaves them undispatched for the existing governed resume
path. A resumable `RUNNING` hold may recreate them only after the barrier clears.

The provider-free abort is legal only while the locked RuntimeExecution is `PREPARED` and its
current owner exactly matches the locked current Attempt/fencing token. It records bounded
control-plane evidence, not a provider cancellation receipt, and creates no lifecycle operation.
The dedicated `abort_before_dispatch` domain transition sets the RuntimeExecution phase to
`CANCELED` with reason `runtime.dispatch_aborted`; it bypasses the provider-terminal validator and
its cancel-intent requirement because the same locked transaction proves that no adapter call was
authorized. Ordinary provider `CANCELED` observations still require a persisted cancel intent.
An absent RuntimeExecution is treated the same way only when the locked Run has no other active or
unresolved RuntimeExecution. Accounting releases the unspent reservation and quota exactly once;
there is no usage settlement because provider dispatch did not occur.

Runtime preparation and dispatch use two fenced aggregate commands:

1. `prepare_runtime_assignment` locks the coordinated aggregate in the fixed order, verifies the
   current Attempt, authority cohort, Runtime Version, Task state, and active drain, then creates or
   validates the `PREPARED` RuntimeExecution and immutable Assignment snapshot. Any active drain or
   persisted CANCEL intent rejects preparation.
2. `cross_runtime_dispatch_boundary` reacquires the same aggregate locks immediately before the
   adapter call. It requires the same current running Attempt/fence, a `PREPARED` execution and exact
   Assignment snapshot, no active drain, no CANCEL intent, and no terminal/unknown evidence. It
   atomically changes the RuntimeExecution to `DISPATCHING` and commits.
3. The adapter may be invoked only after step 2 commits successfully. A stale in-memory Task, Run,
   Attempt, or Assignment is never sufficient authority to call it.

The Task row serializes the drain/dispatch race:

- if the drain transaction wins before the boundary CAS, it aborts absent/`PREPARED` work and the
  CAS returns `ABORTED_BY_DRAIN`; the Worker consumes its `RunRequested` Inbox item as stopped and
  makes zero adapter calls;
- if the boundary CAS commits first, the execution is durably crossed. A later stopping drain sees
  `DISPATCHING`, creates the stable CANCEL intent, and never rewrites the Attempt as proof of
  provider stop. The already-authorized adapter call uses the stable dispatch key; its handle,
  terminal result, response loss, or crash is processed by the ordinary crossed-execution rules;
- a crash after the boundary CAS but before the adapter call remains conservatively crossed and
  parks as outcome unknown after recovery evidence/deadline. It is never blindly redispatched.

There is no state in which a drain can commit a provider-free abort while the same execution can
also commit `DISPATCHING`: both transitions lock and compare the same Task, drain, Attempt fence, and
RuntimeExecution version.

### 8.5 Supervisor unknown outcome

The Supervisor is the sole active Task Run after all Subtasks complete. It uses the reviewed-like
single-Run hold:

- Task/Run/Attempt/Runtime park together;
- confirmed success completes the Task;
- confirmed failure/timeout/unrequested cancellation fails the Task;
- no Subtask status is rewritten.

### 8.6 Domain additions

Add explicit methods rather than ordinary-transition escape hatches:

```text
Subtask.require_runtime_reconciliation(run_id, reason)
Subtask.reconcile_runtime_succeeded(run_id, output)
Subtask.reconcile_runtime_failed(run_id, reason)
Subtask.reconcile_runtime_canceled(run_id, reason)
Subtask.release_never_dispatched_run(run_id)

Task.require_reviewed_runtime_reconciliation(...)
Task.require_coordination_runtime_reconciliation(...)
Task.resume_coordination_after_runtime_reconciliation()
Task.fail_coordination_after_runtime_reconciliation(reason)
```

Ordinary `complete/fail/cancel` methods must not silently accept reconciliation states.

`Task.require_coordination_runtime_reconciliation` accepts `RUNNING ->
RECONCILIATION_REQUIRED` and idempotent `RECONCILIATION_REQUIRED -> RECONCILIATION_REQUIRED` only
when the same active drain is locked. The first call writes the generic
`coordination.runtime_reconciliation_required` Task error; later Subtask parks do not overwrite it.
Per-Subtask status/error and Run/Runtime evidence are the barrier truth. A repeated park for the
same execution/observation is side-effect free; a different conclusion is a conflict.

## 9. Runtime lifecycle and sibling safety

Generic managed cancellation and exact handle persistence are A4.2a prerequisites, before reviewed
admission. Reviewed user cancellation and coordinated sibling drain use the same path; no mode may
mark database state as proof that provider work stopped.

### 9.1 Exact handle binding

Add an immutable `runtime_handle_snapshots` record keyed one-to-one by RuntimeExecution. It stores
the complete bounded canonical `RuntimeExecutionHandle` payload and digest, including its own
`created_at`; RuntimeExecution `created_at/updated_at` are not substitutes. DispatchReceipt
validation requires exact runtime execution, Runtime Version, Assignment ID/digest, canonical
handle digest, and descriptor limits before binding.

After provider dispatch returns a handle, one fenced transaction inserts the immutable snapshot and
updates only the safe RuntimeExecution handle projections. Same execution/same bytes replays;
different bytes conflict. The handle is not a business identifier, never enters model input, and
retains the existing secret-rejection/opaque-reference rules. If dispatch crossed but the process
dies before handle binding, the execution remains uncertain; it is not redispatched.

### 9.2 Lifecycle intent and Outbox contract

Cancellation intent is persisted even when a handle is not yet available:

```text
schema_name: agentmesh.runtime.lifecycle.requested
schema_version: 1
payload: tenant_id, runtime_execution_id, operation_id, operation=CANCEL, deadline
producer: agentmesh-runtime-lifecycle-command-v1
```

The stable operation ID is `runtime-cancel:{runtime_execution_id}:v1`. All Task cancellation,
coordinated failure, budget drain, and operator paths reuse the existing CANCEL intent for that
execution. Cause-specific detail belongs in Task/drain audit, not in operation identity or intent
digest.

The lifecycle row stores `status`, `attempt_count`, `next_attempt_at`, `deadline`,
`claim_token`, `claim_acquired_at`, `claim_expires_at`, `last_error_code`, and an optional validated
receipt summary. `attempt_count` counts provider calls, not queue deliveries. The initial Outbox
message is a durable wake-up, not a request to mint a new operation or message for every retry.

The intent row and initial Outbox message commit before any adapter call. Consumer
`agentmesh-runtime-lifecycle-v1` deduplicates by tenant + consumer + message ID, loads the exact
intent and Run-pinned Runtime Version, and hands it to the same due-operation worker used for
retries. The worker claims due `REQUESTED` rows with `FOR UPDATE SKIP LOCKED`, increments
`attempt_count` only when an exact handle is available, stores a random claim token/lease, commits
the short claim, and then:

1. if no handle snapshot exists, leaves the intent `REQUESTED` and schedules bounded retry until
   deadline without incrementing the provider-call count; it never invents a provider call or
   receipt;
2. otherwise reconstructs the exact handle snapshot and calls `request_cancel` outside a UoW with
   the stable operation ID/deadline;
3. validates receipt operation ID, execution ID, operation, accepted flag, phase, and bounded safe
   summary;
4. atomically stores `ACCEPTED` or `REJECTED` plus receipt summary; initial message consumption is
   completed independently of future due-row retries;
5. on transport/response loss, leaves `REQUESTED` and retries the same operation ID. The provider
   may be called again, but its idempotency contract must produce one cancellation effect;
6. after a persisted receipt, replay consumes Inbox without another provider call.

Retry delay is deterministic exponential backoff from 1 second, doubling to a 60-second cap and
clamped to the operation deadline; deterministic test jitter may only reduce the delay. A transport
failure stores one bounded static `last_error_code` and schedules the same row/operation ID. No
retry creates another lifecycle row or another cancellation identity. The due worker revalidates
status, deadline, and claim token after every call. A live unexpired claim is not due. The adapter
call timeout is strictly shorter than the claim lease; completion compares and clears the exact
claim token. If the process dies, the expired claim becomes due and retries the same operation ID.
Thus concurrent wake-ups produce at most one live in-flight call per operation in the reference
deployment. Provider idempotency remains mandatory because a process can die after the provider
effect and before storing the receipt. A no-handle pass takes no provider-call claim and increments
no `attempt_count`; it only advances `next_attempt_at` under the row lock.

`LifecycleReceipt.observed_phase` follows a closed matrix. For `accepted=true`, it is
`CANCEL_REQUESTED` or a terminal phase. For `accepted=false`, it is a crossed nonterminal phase
(`DISPATCHING`, `ACCEPTED`, `RUNNING`, `WAITING_INPUT`, `WAITING_APPROVAL`, `PAUSE_REQUESTED`, or
`PAUSED`) or a terminal phase. `PREPARED`, an unknown phase, a mismatched operation/execution, and
an absent phase, and `accepted=false` plus `CANCEL_REQUESTED` are protocol conflicts and do not
advance the operation. An invalid receipt clears only the matching claim token, stores
`runtime.lifecycle_receipt_invalid`, and schedules the same operation with the ordinary bounded
backoff; it never waits for its own claim lease to expire. A receipt is lifecycle evidence only:
even when it reports a terminal phase, business finalization requires a separately validated
RuntimeObservation from dispatch/inspect.

No new lifecycle `UNKNOWN` enum is needed: lack of a durable receipt remains `REQUESTED`. At the
deadline, a reconciler selects every `{REQUESTED, ACCEPTED, REJECTED}` lifecycle row whose Runtime
is still nonterminal; acceptance/rejection is never treated as proof that execution stopped. It
claims with the same row lease/CAS as the due worker. A live claim is allowed to finish until its
strictly bounded lease; after claim expiry, exactly one deadline worker performs one final inspect
outside locks and completes only if its claim token still matches. A proven terminal observation
uses ordinary fenced finalization. Otherwise one transaction marks lifecycle `EXPIRED`, records
`runtime.cancel_outcome_unknown`, conservatively settles/releases the still-current Attempt once,
and emits reconciliation required. For an active Task it parks Runtime/Run/Attempt plus the
reviewed Task or coordinated Subtask and re-enters the coordinated barrier when applicable. If the
Task is already `CANCELED`, it parks only Runtime as `OUTCOME_UNKNOWN`; Run/Attempt remain
`CANCELED` and the Runtime-only reconciliation matrix below applies.

A canceled terminal Task remains canceled while its Runtime-only uncertainty is closed. The exact
mode-specific reconciliation target is:

| Required persisted precondition | Proven Runtime conclusion | Business result |
|---|---|---|
| Task/Run/Attempt are `CANCELED`; Runtime is `LOST` or `OUTCOME_UNKNOWN`; stable CANCEL intent exists | `SUCCEEDED` | converge Runtime only; retain late output as quarantined Runtime evidence |
| same | `FAILED` | converge Runtime only |
| same | `TIMED_OUT` | converge Runtime only |
| same | `CANCELED` | converge Runtime only |

All four branches keep Task, Run, and Attempt `CANCELED`; preserve Task output/error/current-Run
projection; schedule no successor/reviewer/Supervisor; capture no transactional Memory or research;
materialize no Artifact; and make no quota/budget mutation. A4.2 terminal usage is empty, and the
reservation was already released by cancellation/expiry, so a second settlement is forbidden.
Success output remains evidence only and is never promoted to the canceled Task.

For that success branch only, the already-required immutable reconciliation observation stores the
canonical mapping under internal `runtime_observations.evidence.quarantined_output`. The complete
evidence JSON remains under the existing 65,536-byte canonical limit; no second unbounded payload
column is introduced. This key is omitted from ordinary Runtime observation API projections and is
readable only by the privileged `OUTCOME_RECONCILE` path. It is never copied to Assignment/model
input, Task/Run output, Artifact, Memory, research, Outbox, log, trace, or incident projection.
Artifact references, if present in later contract versions, are retained only as opaque bounded
evidence refs under the same limit and are not materialized.

The privileged command uses the existing strict evidence, idempotency, tenant, ownership/fence, and
competing-conclusion checks. It reuses the existing
`RECONCILE_RUNTIME_{SUCCEEDED|FAILED|CANCELED|TIMED_OUT}` action and writes immutable resolution
details with `target_kind=canceled_task_runtime_only`; no new action enum or database constraint is
introduced. It emits
`agentmesh.runtime.canceled-task-outcome-reconciled`. Exact replay returns the stored resolution;
a different conclusion conflicts. This branch is the only legal way to converge its Runtime and
must not call ordinary Task/Run/Attempt completion methods.

### 9.3 Runtime capability behavior

The generic subprocess `managed_async` fixture exercises active cancellation, response loss,
deadline parking, and same-operation replay. The current LangGraph v2 inline adapter does not
return a handle until execution is terminal, so A4.2 must not claim active LangGraph cancellation:
the coordinated barrier truthfully drains such a sibling to terminal without scheduling successors
or marking it stopped. If a handle later binds after an earlier intent, the lifecycle consumer may
process it if still nonterminal.

A4.2 provides the test-only lifecycle control-plane protocol and deterministic provider proof.
A4.3 supplies production-durable state, lifecycle workers, and a LangGraph `managed_async`
implementation before any production cutover.

## 10. Persistence and compatibility

Expected schema change:

- add immutable `runtime_assignment_snapshots` keyed by RuntimeExecution, with canonical payload
  and digest constraints;
- add immutable `runtime_handle_snapshots` keyed by RuntimeExecution, with complete canonical
  handle payload/digest including handle `created_at`;
- expand `runtime_lifecycle_operations` with durable due-worker fields (`attempt_count`,
  `next_attempt_at`, `claim_token`, `claim_acquired_at`, `claim_expires_at`, and bounded
  `last_error_code`) before the lifecycle writer is enabled;
- add `runtime_integrity_incidents` for late conflicting terminal evidence, including its closed
  state constraint and four-column conflict uniqueness constraint;
- add append-only `runtime_integrity_incident_actions` before exposing incident state commands;
- add `coordination_runtime_drains` for multi-Run convergence intent;
- expand `ck_subtasks_status` with `RECONCILIATION_REQUIRED`.

No Task or Run authority rewrite is permitted. The migration is split:

1. A4.2a.0 expand-only tables, lifecycle due/claim columns, plus readers/repositories; no writer
   behavior;
2. A4.2a.1 Assignment/handle/lifecycle/integrity writers and shared DIRECT-safe semantics;
3. A4.2c.1 Subtask reader/domain compatibility and expand-only status constraint;
4. A4.2c.2 coordinated drain/status writers and cutover gate.

Before any new table contains a row and before the new Subtask status is written, down migration is
supported. A down migration refuses without data loss when any Assignment snapshot, handle
snapshot, integrity incident, coordination drain, or `RECONCILIATION_REQUIRED` Subtask exists.

Lifecycle expansion is old-writer compatible: `attempt_count` is non-null with server default `0`;
`next_attempt_at`, `claim_token`, `claim_acquired_at`, `claim_expires_at`, and `last_error_code` are
nullable; existing rows are backfilled with `attempt_count=0` and otherwise remain unchanged.
A4.2a.0 readers interpret null `next_attempt_at` as not scheduled and expose none of these fields
through the existing public DTO. A4.2a.1 writer activation first schedules eligible existing
operations explicitly. Contract or down migration refuses if any row has nonzero `attempt_count`,
a due/claim/error field, or another A4.2 lifecycle writer marker; dropping default-only untouched
columns remains safe before writer activation. Claim token, acquired time, and expiry are either
all null or all non-null; the database enforces `claim_expires_at > claim_acquired_at`.

After an Assignment snapshot is written for a crossed execution, its expand migration is the
database floor because deleting the only canonical Assignment would make replacement unsafe. The
application rollback floor is A4.2a.1, which can load and honor snapshots/lifecycle even with both
orchestrated gates off. After reviewed authority is written, the application floor advances to the
A4.2b reader/finalizer. After coordinated status/drain values are written, the database floor
includes the coordinated expand migration and the application floor advances to A4.2c.2. Real
PostgreSQL tests prove every pre-write downgrade and post-write refusal.

If implementation proves a persisted cohort field is necessary, stop and amend this design before
adding it. Do not introduce a mutable Task-level Runtime switch as an implementation shortcut.

Because revision `20260825_0049` was deployed as the reader-only floor before A4.2a.1, the action
ledger and the tighter accepted-terminal incident constraint are delivered by a following
expand-compatible migration. Its downgrade refuses while any action row exists, then removes only
the action ledger and restores the prior reader constraint. The existing `0049` downgrade remains
responsible for refusing loss of any incident evidence row.

## 11. Fixed lock order and transaction boundaries

For a single active reviewed/Supervisor Run:

```text
Task -> pinned RuntimeVersion -> Run -> latest Attempt -> RuntimeExecution
-> Assignment snapshot -> handle snapshot -> lifecycle/incident rows
```

For coordinated parking/convergence:

```text
Task
-> active drain when present
-> pinned RuntimeVersions ordered by UUID
-> Subtasks ordered by UUID
-> Runs ordered by UUID
-> latest Attempts in Run order
-> RuntimeExecutions in Run order
-> Assignment/handle snapshots in execution order
-> lifecycle/incident rows in execution order
```

Read-only location may occur before locking, but every identity and version is revalidated after
locks. Transactions that need the active drain create/lock it immediately after the Task, then
acquire the remaining rows in the listed order; the implementation must use one helper so it never
locks a target Run and later expands to the aggregate. Adapter
validate/dispatch/inspect/lifecycle calls occur with no database lock held.

Coordinated Runtime preparation, provider-free abort, and dispatch-boundary CAS use this same
aggregate helper and order. The boundary transaction commits `DISPATCHING` before the adapter is
called; the adapter is never invoked from inside the transaction. Direct/reviewed single-Run
preparation uses the single-active order and applies the equivalent Attempt/fence, cancellation, and
Task-state guards.

Transactions atomically include the applicable business state, Runtime evidence, lifecycle intent,
Inbox/Outbox, idempotency record, accounting mutation, and transactional Memory capture.

## 12. Implementation slices

### A4.2a.0 — expand compatibility, no writers

- add Assignment snapshot, handle snapshot, and Runtime integrity incident tables;
- add/backfill nullable/default lifecycle due-worker columns and old-reader/new-reader compatibility
  tests without scheduling or claiming an operation;
- add bounded domain/read/repository support and pre-write downgrade tests;
- expose no writer behavior and do not change execution authority.

### A4.2a.1 — shared semantics and lifecycle, no new orchestrated admission

- close #154 with one terminal validator;
- add canonical application work-item builder and use it on legacy + managed paths;
- persist/load immutable Assignment and exact handle snapshots for managed replacement/lifecycle;
- add forced-conflict parking, late-terminal integrity incidents, generic lifecycle Outbox consumer,
  deadline parking, and Runtime-only convergence for an already canceled Task;
- extract shared business outcome applier;
- add authority-cohort resolver and inheritance tests;
- keep both new cutover gates absent/disabled;
- prove existing DIRECT/reviewed/coordinated legacy behavior is unchanged.

### A4.2b — reviewed admission and reconciliation

- add reviewed cutover gate and startup guard;
- admit first reviewed executor and inherit into reviewer/revision Runs;
- pass exact work item to Assignment and backend;
- extend parking/reconciliation to executor and reviewer roles;
- route reviewed user cancellation through the A4.2a.1 lifecycle protocol and prove late results
  cannot overwrite the canceled Task;
- add real PostgreSQL end-to-end and rollback tests;
- keep server gate disabled.

### A4.2c.1 — coordinated reader compatibility

- Add `RECONCILIATION_REQUIRED` to `SubtaskStatus` and to the ORM/database
  `ck_subtasks_status` constraint. It remains nonterminal and is never included in
  `TERMINAL_SUBTASK_STATUSES`.
- Existing repository and API projections must round-trip a row already carrying the new value,
  but no application service may create that value in this slice. Scheduling continues to select
  only `READY`; completion/failure continues to require `RUNNING`.
- Ordinary Subtask transitions, including `cancel`, must reject a reader-loaded
  `RECONCILIATION_REQUIRED` Subtask. The explicit park/reconcile/release methods in §8.6 land only
  with A4.2c.2, so c.1 cannot become an accidental writer through an existing broad transition.
- Add one expand-only Alembic revision after 0050. Upgrade replaces only
  `ck_subtasks_status`, preserving every row and index. Downgrade first checks for any
  `RECONCILIATION_REQUIRED` row and refuses before DDL with a bounded migration error; when none
  exists it restores the exact pre-c.1 constraint.
- PostgreSQL qualification must prove: existing-status upgrade preservation; ORM/migration
  constraint parity; repository/API read round-trip; clean downgrade to 0050; post-write downgrade
  refusal with schema version and data unchanged; and successful downgrade after scoped cleanup.
- This slice adds no drain table or row, lifecycle behavior, feature gate, startup guard, Worker
  branch, authority selection, finalizer/reconciler behavior, Runtime SDK field, or adapter call.
  Default profiles and the server therefore remain unchanged.

The pre-write database rollback floor remains 0050. Once a Subtask row contains the new status,
0051 is the database floor and A4.2c.1 is the application reader floor. A4.2c.1 is complete only
when the full non-PostgreSQL suite, the focused real-PostgreSQL migration matrix, architecture
imports, Ruff, and `alembic check` are green. It must land independently of A4.2c.2.

### A4.2c.2 — coordinated writer and convergence barrier

#### A4.2c.2a — drain reader/schema floor, no behavior writer

This slice lands independently before any coordinated Runtime writer. It adds the durable object
that later barrier transactions will lock, while leaving coordinated admission, parking,
cancellation fan-out, reconciliation, scheduling, default profiles, and server configuration
unchanged. Merely deploying 0052 must therefore produce zero `coordination_runtime_drains` rows.

Add `CoordinationRuntimeDrain`, `CoordinationRuntimeDrainStatus`, and
`CoordinationRuntimeDrainTarget` to `agentmesh.domain.coordination`. The closed values are:

- status: `DRAINING`, `COMPLETE`;
- target: `RUNNING`, `WAITING_APPROVAL`, `FAILED`, `CANCELED`.

The aggregate fields are exactly `id`, `tenant_id`, `task_id`, `triggering_run_id`, `target`,
`reason`, `status`, `version`, `created_at`, `updated_at`, and `completed_at`. IDs are UUIDs;
`tenant_id` contains 1-128 characters; the normalized reason contains 1-4096 characters; version
is positive; and all timestamps are timezone-aware UTC. `updated_at >= created_at` is mandatory.
`DRAINING` requires `completed_at IS NULL`; `COMPLETE` requires a non-null
`completed_at >= created_at` and `updated_at >= completed_at`. The reader must reject malformed
persisted projections rather than repairing them. Domain transitions that create, retarget, or
complete a drain remain absent until c.2b, so c.2a is not an accidental behavior writer.

Add `coordination_runtime_drains` in revision `20260909_0052`, directly after 0051, with the exact
fields above. `task_id` references `tasks(id)` with `ON DELETE CASCADE`; `triggering_run_id`
references `task_runs(id)` with `ON DELETE RESTRICT`. Database checks mirror the closed status,
target, bounded nonblank tenant/reason, positive version, timestamp ordering, and
status/completion coupling. Add:

- a partial unique index `uq_coordination_runtime_drains_active_task` on `task_id` where
  `status = 'DRAINING'`, allowing history but at most one active drain per Task;
- `ix_coordination_runtime_drains_tenant_status_updated` on
  `(tenant_id, status, updated_at)`;
- `ix_coordination_runtime_drains_task_created` on `(task_id, created_at)`.

The repository port is separate from `RuntimeRepository` and is exposed as
`UnitOfWork.coordination_runtime_drains`. It has only the storage operations required by later
aggregate transactions:

```text
add(value)
get(id, *, tenant_id, for_update=False)
get_active_for_task(task_id, *, tenant_id, for_update=False)
list_for_task(task_id, *, tenant_id)
save(value, *, tenant_id)
```

All reads join through the owning Task and require both the drain and Task tenant to match.
`get_active_for_task` returns only `DRAINING`; a corrupt duplicate must fail rather than selecting
one. `list_for_task` is ordered by `(created_at, id)`. `save` first performs a scoped locked read,
updates all mutable projection fields, and uses the existing SQLAlchemy version/CAS convention.
Repository `add` and `save` exist to qualify the storage contract, but no production application
service may call either in c.2a. No REST/GraphQL endpoint or Outbox event is added in this slice.

The 0052 downgrade first checks for any drain row and refuses before issuing DDL when one exists.
When the table is empty it removes only the three indexes and the table, returning exactly to
0051. A refused downgrade must leave schema, constraints, indexes, and rows unchanged; deleting
the test row must then allow downgrade and re-upgrade. The rollback floor remains 0051 until the
first drain is written; afterwards 0052 is the database floor and c.2a is the application reader
floor even while the coordinated gate is off.

c.2a acceptance is closed by domain/unit tests plus real PostgreSQL tests proving: valid
round-trip; tenant isolation including mismatched duplicated tenant data; locked/unlocked reads;
deterministic history ordering; active partial uniqueness; ORM/migration constraint and index
parity; every invalid status/target/reason/version/timestamp combination rejected; clean
`0052 -> 0051 -> 0052`; post-write downgrade refusal before DDL; cleanup then successful
downgrade; zero production call sites for `add`/`save`; full non-PostgreSQL and PostgreSQL suites;
Ruff, architecture imports, `alembic check`, and `git diff --check`.

#### A4.2c.2b1 — closed domain transitions and dispatch classification

This slice adds pure domain behavior only. It changes no repository schema, UoW orchestration,
application service, worker, gate, scheduler, accounting, lifecycle row, Inbox/Outbox event, or
adapter call. Production code outside the domain must have zero call sites for the new mutating
methods until c.2b2 installs the single aggregate lock boundary.

`CoordinationRuntimeDrain` remains frozen and gains functional transitions returning either `self`
for an exact replay/retained first cause or a new projection with `version + 1`:

```text
CoordinationRuntimeDrain.start(
    *, drain_id, tenant_id, task_id, triggering_run_id, target, reason, at
)
CoordinationRuntimeDrain.retarget(*, target, reason, at)
CoordinationRuntimeDrain.complete(*, at)
CoordinationRuntimeDrain.stopping -> bool
```

`start` accepts the same strict IDs, normalized bounded strings, enum values, and UTC policy clock
as the reader projection and creates version 1 `DRAINING` state. `retarget` requires `DRAINING`.
The precedence lattice is closed: `RUNNING` may advance to `WAITING_APPROVAL`, `FAILED`, or
`CANCELED`; `WAITING_APPROVAL` may advance to `FAILED` or `CANCELED`; `FAILED` and `CANCELED` are
terminal targets and first terminal cause wins. A request for the current target returns `self` and
retains the original reason; a lower/incomparable request after a terminal target also returns
`self`. No transition can change `FAILED` to `CANCELED` or the reverse. `complete` requires
`DRAINING`, sets `COMPLETE`, `updated_at`, and `completed_at` to the caller clock, and increments
version; replay on `COMPLETE` returns `self`. Every non-replay clock must be timezone-aware UTC and
not precede `updated_at`. `stopping` is true exactly for `WAITING_APPROVAL|FAILED|CANCELED`.

`Subtask` gains only the explicit reconciliation/undispatched methods already named in §8.6:

```text
require_runtime_reconciliation(run_id, reason, *, at)
reconcile_runtime_succeeded(run_id, output, *, at)
reconcile_runtime_failed(run_id, reason, *, at)
reconcile_runtime_canceled(run_id, reason, *, at)
release_never_dispatched_run(run_id, *, at)
```

Every method validates the current Run binding and caller-owned monotonic UTC clock.
`require_runtime_reconciliation` accepts only `RUNNING`; it sets
`RECONCILIATION_REQUIRED`, clears output, stores the normalized 1-4096 character safe reason, and
touches once. An exact replay with the same Run and reason is side-effect free; a different reason
while already parked is a conflict. Reconcile methods accept only `RECONCILIATION_REQUIRED` and
retain the Run binding as immutable evidence while producing the corresponding terminal status.
Success requires a mapping output and clears error; failure/cancel require a safe reason and clear
output. `release_never_dispatched_run` accepts `READY` with a queued binding or `RUNNING` with an
active binding, returns the Subtask to `READY`, clears `current_run_id`, output, and error, and
touches once. It rejects terminal/reconciliation states and an unbound or different Run.

`Task` gains the first hold/apply methods from §8.6. Each accepts the locked drain projection so
the domain can validate `tenant_id`, `task_id`, drain status, and target rather than trusting a
bare UUID:

```text
require_coordination_runtime_reconciliation(drain, *, at)
resume_coordination_after_runtime_reconciliation(drain, *, at)
fail_coordination_after_runtime_reconciliation(drain, *, at)
```

They require `execution_mode=COORDINATED` and no Task `current_run_id` (Supervisor holds remain the
single-Run path). Requiring the hold accepts `RUNNING` plus a `DRAINING` drain, moves to
`RECONCILIATION_REQUIRED`, clears output, and writes only
`coordination.runtime_reconciliation_required`; a replay while held is side-effect free.
Resume requires a `COMPLETE/RUNNING` drain and restores `RUNNING` with no output/error. Fail
requires a `COMPLETE/FAILED` drain, sets `FAILED`, uses the drain reason, and clears output. User
cancellation and `WAITING_APPROVAL` application remain c.2f and cannot call these methods as a
shortcut.

Add one pure classifier in `agentmesh.domain.coordination`:

```text
classify_runtime_boundary(*, subtask, run, latest_attempt, executions)
    -> CoordinationRuntimeBoundary
```

The closed result values are `NOT_CROSSED_QUEUED`, `NOT_CROSSED_NO_EXECUTION`,
`NOT_CROSSED_PREPARED`, `CROSSED_ACTIVE`, `KNOWN_TERMINAL`, and
`RECONCILIATION_EVIDENCE`. The classifier first validates a managed EXECUTOR Run bound to the same
Task/Subtask, exact `current_run_id`, latest Attempt ownership, Runtime Run identity, execution
intent/binding, and at most one active-or-unresolved execution. It then maps the §8.4 table exactly:

- queued Run, no running Attempt, and no active/unresolved execution -> `NOT_CROSSED_QUEUED`;
- running Run plus running latest Attempt and no execution -> `NOT_CROSSED_NO_EXECUTION`;
- the same chain plus exact-owner/fence `PREPARED` -> `NOT_CROSSED_PREPARED`;
- `DISPATCHING|ACCEPTED|RUNNING|WAITING_INPUT|WAITING_APPROVAL|PAUSE_REQUESTED|PAUSED|
  CANCEL_REQUESTED` -> `CROSSED_ACTIVE`;
- `SUCCEEDED|FAILED|CANCELED|TIMED_OUT` -> `KNOWN_TERMINAL`;
- `LOST|OUTCOME_UNKNOWN` -> `RECONCILIATION_EVIDENCE`.

Every unlisted mixed state fails closed with `InvalidTaskTransition`; classification never mutates
an input. `RuntimeExecution.abort_before_dispatch` remains the sole PREPARED execution mutation
and is not called in b1.

b1 acceptance requires an exhaustive table-driven unit matrix for drain precedence/replays,
Subtask and Task transitions, all Runtime phases, wrong tenant/identity/role/authority/fence,
multiple unresolved executions, monotonic clocks, and input immutability. An AST guard proves zero
new production call sites. Full non-PostgreSQL tests, Ruff, architecture imports, and
`git diff --check` must pass. PostgreSQL behavior and migration head must remain byte-for-byte
unchanged; b1 needs no new migration.

#### A4.2c.2b2 — one coordinated aggregate lock boundary

This slice installs the sole transaction-local reader/locker used by every later coordinated
managed writer. It does not start or retarget a drain, mutate Task/Subtask/Run/Attempt/Runtime
state, create lifecycle operations, emit events, call an adapter, or change admission. The public
server gate remains absent/disabled and migration head remains 0052.

Add `CoordinatedRuntimeAggregateLocker` in `agentmesh.application.coordinated_runtime`. Its only
entry point is:

```text
lock(uow, *, tenant_id, task_id) -> CoordinatedRuntimeAggregate
```

The result is a frozen transaction-scoped projection containing the locked Task, optional active
drain, resolved immutable `AuthorityCohort`, Runtime Versions keyed by ID, all Subtasks and Runs in
UUID order, the latest Attempt for each Run in Run order, every RuntimeExecution grouped in Run
order and then execution UUID order, Assignment/handle snapshots and lifecycle/integrity rows in
execution order, and one boundary classification for each currently bound managed EXECUTOR
Subtask Run. It must never be cached or used after the owning UoW exits. The helper returns domain
objects for later in-transaction commands; it performs no save itself.

Repository protocols gain only explicit lock-capable reads needed by this helper. Existing broad
read methods keep their behavior. New/extended reads must be tenant scoped through the Task join
where applicable and use deterministic ordering:

```text
RuntimeRepository.list_executions_for_run(run_id, *, tenant_id, for_update=False)
RuntimeRepository.get_assignment_snapshot(execution_id, *, tenant_id, for_update=False)
RuntimeRepository.get_handle_snapshot(execution_id, *, tenant_id, for_update=False)
RuntimeRepository.list_lifecycle_operations(execution_id, *, tenant_id, for_update=False)
RuntimeRepository.list_integrity_incidents_for_execution(
    execution_id, *, tenant_id, for_update=False
)
```

`TaskRunRepository.list_for_task(..., for_update=True)` and
`SubtaskRepository.list_for_task(..., for_update=True)` must order by UUID, not creation time or
business key. If changing an existing reader's documented presentation order would be observable,
add private/exact `lock_for_task_ordered` methods instead. Runtime Version locks are acquired by
calling the already scoped `get_version(..., for_update=True)` for sorted distinct UUIDs. Latest
Attempts are acquired by `latest_for_run(..., for_update=True)` in locked Run order. Repository
implementations must use `SELECT ... FOR UPDATE` on PostgreSQL; SQLite may preserve its existing
transaction semantics while returning the identical ordered projection.

The helper follows §11 exactly and never locks one target Run before expanding to the aggregate:

```text
Task
-> active drain, when present
-> distinct pinned RuntimeVersions by UUID
-> Subtasks by UUID
-> Runs by UUID
-> latest Attempts in Run order
-> RuntimeExecutions in Run order, then UUID
-> Assignment then handle snapshot in execution order
-> lifecycle operations then integrity incidents in execution order, each by UUID
```

Unprotected discovery reads may determine candidate IDs only after the Task lock. After all locks
are acquired, the helper repeats membership reads and rejects with `RuntimeExecutionConflict` if
the active drain identity/version, Subtask IDs, Run IDs, current Run bindings, latest Attempt IDs,
RuntimeExecution IDs, or dependent-row IDs changed. A missing row, duplicate ID, cross-tenant row,
wrong Task/Run/Subtask/execution relationship, or a membership phantom is a conflict; callers retry
the whole UoW and never continue with a partial aggregate.

The locked Task must be `COORDINATED`. Every local Run participates in exactly one cohort resolved
from persisted Runs, with no mixed `runtime_authority`, comparison mode, or managed Runtime Version.
Managed Runs require their pinned Runtime Version to exist under the same tenant; the helper locks
that version even for terminal/history Runs. A currently bound Subtask Run must be an EXECUTOR Run
whose Task/Subtask/current-run identities are exact. Managed current Runs are classified only by
the b1 pure classifier. Legacy current Runs are retained in the projection but have no managed
boundary classification. A mixed legacy/managed aggregate fails before the projection is returned.
Supervisor Runs without a Subtask are retained and cohort-validated but are not passed to the
Subtask classifier.

Snapshot, lifecycle, and incident absence is valid. Presence must bind to the exact locked
execution and tenant. At most one Assignment and one handle snapshot may exist per execution.
Lifecycle and incident rows are returned in stable UUID order without interpreting their outcome;
later slices remain responsible for lifecycle and convergence behavior. The helper must not use
`SKIP LOCKED`, advisory locks, process-local mutexes, or a second lock order.

b2 acceptance includes unit fakes that record and assert the exact repository-call order; immutable
ordered projection tests; wrong tenant/Task/mode/cohort/role/binding/version/fence and phantom
membership rejection; all b1 classifier results reached through the helper; and an AST guard
proving there is one production aggregate-lock implementation and zero production behavior writers
or callers outside its tests. Real PostgreSQL tests use two concurrent transactions to prove the
Task lock serializes aggregate readers, reverse input/insertion order still produces the same lock
order, tenant-mismatched duplicate-looking data is invisible, and two helpers complete without a
deadlock. The full non-PostgreSQL and PostgreSQL suites, Ruff, architecture imports,
`alembic check`, and `git diff --check` must pass. No migration or feature/configuration change is
permitted in b2.

#### A4.2c.2c — coordinated admission and dispatch-boundary foundation

The earlier coarse plan activated coordinated admission in this slice. That ordering is unsafe:
the c.2d/c.2e barrier and reconciliation appliers would not yet exist, so an opt-in Task could be
dispatched but could not safely consume its result. The closed implementation order is therefore:

1. **c.2c1 — closed gate and cohort candidate:** add the feature vocabulary, dependency/startup
   validation, and pure candidate selection, but do not let scheduling create a managed coordinated
   Run;
2. **c.2c2 — aggregate prepare command:** atomically create/claim `PREPARED` plus the immutable
   Assignment under the b2 lock helper, with zero production callers;
3. **c.2c3 — aggregate dispatch-boundary CAS:** atomically authorize exactly one
   `PREPARED -> DISPATCHING` transition, with zero adapter calls and zero production callers;
4. c.2d and c.2e add known/unknown outcome convergence; c.2f alone connects admission and the
   Worker, then qualifies lifecycle, cancellation, pause, budget, concurrency, and parity.

This is a sequencing correction, not a scope reduction. The public server and all default profiles
remain off throughout, and no user can create a half-supported managed coordinated Task.

##### c.2c1 — closed gate and cohort candidate

Add `Feature.MANAGED_RUNTIME_COORDINATED_CUTOVER` with dependencies on
`MANAGED_RUNTIME_WORKER` and `COORDINATED_EXECUTION`. `_validate_managed_cutover_config` treats it
like the DIRECT/REVIEWED cutover gates: test/testing environment and deterministic provider only.
Until c.2f, startup must additionally reject an enabled coordinated gate with the bounded reason
`managed_runtime_coordinated_cutover is not activation-ready`; this explicit guard is removed only
in the c.2f activation commit.

`AuthorityCohortResolver` gains a pure/internal coordinated candidate selector that, given an
already locked Task and the already validated built-in LangGraph v2 Runtime Version, returns the
same Task-bound managed `AuthorityCohort` used by later initial admission. Existing
`initial_admission_in_uow` continues returning legacy for `COORDINATED` in c.2c1. Continuations
remain governed by persisted cohort inheritance; no mutable Task-level authority field is added.
An AST guard proves the candidate is not a scheduling call site.

##### c.2c2 — aggregate prepare command

Add `CoordinatedRuntimeDispatchService`, backed by a UoW factory and the b2 locker, with this first
command:

```text
prepare_runtime_assignment(
    *, tenant_id, task_id, run_id, attempt_id, fencing_token,
       assignment, now
) -> CoordinatedRuntimePrepareResult

CoordinatedRuntimePrepareResult.kind = PREPARED | REPLAY | BLOCKED_BY_DRAIN
```

Inputs are IDs and immutable Runtime Assignment bytes, never caller-owned Task/Run projections.
The command opens one UoW, calls the b2 locker first, and selects the exact current Subtask/managed
EXECUTOR Run/latest running Attempt. It requires the Task to be `RUNNING`, the Run/Subtask chain to
be active, the Task-bound managed cohort and Runtime Version to match the Assignment, the execution
ID to equal `runtime_execution_intent_id`, and the attempt ID/fence/lease to be current at `now`.
`now` is caller-owned, timezone-aware UTC, monotonic against every locked row it changes.

The canonical Assignment validator used by DIRECT/REVIEWED preparation is extracted as a pure
shared validator; coordinated code must not copy or weaken identity, agent-version digest,
work-item, contract, or Runtime-Version checks. The stable dispatch key remains
`runtime-dispatch:{tenant_id}:{execution_id}` and its digest uses the existing canonical formula.
The locked Runtime Version may be `PUBLISHED` or `DEPRECATED`; `DRAFT`, `REVOKED`, a missing
version, or an incompatible descriptor fails before writes. Registration/default validity is an
admission-time c.2f check; dispatch authority is the immutable Run cohort plus its locked Version,
so c.2c must not add a registration row to the §11 lock order. No Run-first registry helper may be
reused.

With no active drain, `NOT_CROSSED_NO_EXECUTION` creates the exact `RuntimeExecution` in
`PREPARED`, claims it to the locked Attempt/fence in the same transaction, binds the Run execution
ID, and inserts the immutable Assignment snapshot. All rows commit atomically. An exact
`NOT_CROSSED_PREPARED` replay with the same execution, owner/fence, dispatch digest, Assignment ID,
digest, and canonical snapshot returns `REPLAY` without touching versions or timestamps. Any
different bytes or identity conflict. `CROSSED_ACTIVE`, known terminal, or reconciliation evidence
never prepares another execution.

If an active drain is locked, the command returns `BLOCKED_BY_DRAIN` with the drain ID/version and
makes no mutation. Provider-free abort, Attempt/accounting release, Run/Subtask release, and Inbox
consumption are deliberately owned by c.2d/c.2f; c.2c2 must not partially implement them. The
result authorizes no adapter call in every case.

The implementation must not call the existing `RuntimeRegistryService.prepare_execution_in_uow`,
whose single-Run lock order begins at Run. Shared pure construction/validation helpers may be
extracted, but all database access goes through the aggregate transaction. There are zero
production callers in c.2c2.

##### c.2c3 — aggregate dispatch-boundary CAS

The same service gains:

```text
cross_runtime_dispatch_boundary(
    *, tenant_id, task_id, run_id, attempt_id, fencing_token,
       runtime_execution_id, assignment_digest, now
) -> CoordinatedRuntimeDispatchResult

CoordinatedRuntimeDispatchResult.kind =
    DISPATCH_AUTHORIZED | ALREADY_CROSSED | BLOCKED_BY_DRAIN
```

The command reacquires the full aggregate through b2. `DISPATCH_AUTHORIZED` is returned only by the
transaction that validates `NOT_CROSSED_PREPARED`, exact current Attempt/owner/fence, exact immutable
Assignment snapshot, `RUNNING` Task/Subtask/Run, no active drain, no CANCEL lifecycle intent, no
terminal/unknown evidence, and an allowed locked Runtime Version, then persists
`RuntimeExecution.phase=DISPATCHING` and commits. It never invokes `validate`, `dispatch`, `inspect`,
or any lifecycle adapter method while locks are held—or anywhere in this service.

An exact replay that finds the same execution already `DISPATCHING` or later returns
`ALREADY_CROSSED`, which explicitly does **not** authorize another provider call. Response loss
after the boundary commit therefore converges through recovery/unknown-outcome handling rather
than redispatch. A stale Attempt/fence, changed Assignment, different execution, terminal evidence,
or reconciliation evidence is a conflict. An active drain returns `BLOCKED_BY_DRAIN` without
changing `PREPARED`; c.2d later performs the provider-free abort. If the boundary CAS commits first,
a later drain must classify the execution as crossed and can only use lifecycle cancellation.

c.2c acceptance requires unit matrices for gate dependencies/startup refusal, candidate purity,
prepare/replay/changed-bytes/stale-owner/version-status/drain results, and the three dispatch result
kinds. Real PostgreSQL barriers prove drain-before-prepare makes no execution/snapshot; drain after
PREPARED prevents the CAS; CAS-before-drain persists `DISPATCHING`; two concurrent CAS calls return
exactly one `DISPATCH_AUTHORIZED`; and simulated response loss never reauthorizes dispatch. Rollback
tests prove execution, Run binding, snapshot, and owner claim are one atomic unit. AST guards prove
zero production admission/Worker/adapter call sites. Full non-PostgreSQL and PostgreSQL suites,
Ruff, architecture imports, `alembic check`, and `git diff --check` pass; migration head stays 0052.

##### c.2d — known-terminal convergence and sibling drain

c.2d is delivered in three independently reviewable commits. It remains disconnected from the
Worker and admission path until c.2f; this lets the transactional protocol be qualified without
making a partially convergent coordinated Runtime reachable by users.

1. **c.2d1 — pure barrier planner:** define the closed plan/result vocabulary and decide sibling
   actions from one already locked aggregate, with zero writes and zero production callers;
2. **c.2d2 — transaction-local barrier applier:** apply a caller-supplied d1 plan to the same locked
   aggregate, including provider-free abort/release and stable cancel intents, but do not open its
   own UoW and do not consume a Runtime observation;
3. **c.2d3 — known-terminal command:** record one canonical known-terminal observation, settle its
   local Run/Attempt/Subtask, invoke d1+d2 in the same aggregate transaction, and either schedule
   ordinary success progression or retain/complete the drain.

No c.2d commit may add a feature-profile default, remove the coordinated startup refusal, call an
adapter, or become a caller of c.2c prepare/CAS. Migration head remains 0052.

###### c.2d1 — pure barrier planner

Add the following closed vocabulary in `agentmesh.application.coordinated_runtime_barrier`:

```text
CoordinatedSiblingActionKind =
    RETAIN_TERMINAL | WAIT_CROSSED | WAIT_RECONCILIATION |
    RELEASE_QUEUED | ABORT_NO_EXECUTION | ABORT_PREPARED | REQUEST_CANCEL

CoordinatedBarrierCompletion =
    CONTINUE_SUCCESS | WAIT_ACTIVE | WAIT_RECONCILIATION |
    APPLY_RUNNING | APPLY_WAITING_APPROVAL | APPLY_FAILED | APPLY_CANCELED

CoordinatedSiblingAction(run_id, subtask_id, execution_id, attempt_id,
                         fencing_token, kind)
CoordinatedBarrierPlan(task_id, tenant_id, triggering_run_id,
                       requested_target, requested_reason,
                       effective_target, effective_reason,
                       create_drain, retarget_drain,
                       sibling_actions, completion)
```

`plan_known_terminal(aggregate, *, triggering_run_id, phase, cancel_intent_present,
safe_error) -> CoordinatedBarrierPlan` accepts only the exact current managed EXECUTOR
Run/Subtask/latest Attempt already selected by the b2 aggregate. `phase` is one of the four
`KnownTerminalPhase` values. `SUCCEEDED` requires no error; `FAILED`/`TIMED_OUT` require or derive a
bounded stable error; `CANCELED` with no matching persisted cancel intent is treated as
`FAILED/runtime.unrequested_cancellation`. A caller boolean alone is not authority: when true, the
aggregate must contain the stable `runtime-cancel:{execution_id}:v1` CANCEL operation bound to the
target execution. A mismatched flag/row is a conflict.

The planner runs after the aggregate lock and before any observation/business mutation. The target
Task must be `RUNNING`, or `RECONCILIATION_REQUIRED` with the same active drain for a late sibling;
its Subtask, Run, and latest Attempt must all be `RUNNING`, and its exact RuntimeExecution boundary
must be `CROSSED_ACTIVE`. `QUEUED`, absent, `PREPARED`, known-terminal, reconciliation-evidence,
stale owner/fence, or incompatible Runtime Version targets fail before a plan is returned.

An ordinary successful Subtask with no active drain produces `CONTINUE_SUCCESS`, no drain, and no
sibling action. It preserves normal parallel DAG execution. A first known failure creates a
`FAILED` drain. When a drain already exists, `CoordinationRuntimeDrain.retarget` precedence decides
the effective target/reason; a late success or cancellation can never replace the first terminal
cause. c.2d1 can retain a pre-existing `RUNNING`, `WAITING_APPROVAL`, or `CANCELED` target for future
c.2e/c.2f callers, but c.2d itself creates only `FAILED`.

For every non-triggering current managed Executor Run, sorted by UUID, map the b1 classification:

| Boundary | RUNNING drain | stopping drain |
|---|---|---|
| `NOT_CROSSED_QUEUED` | `RELEASE_QUEUED` | `RELEASE_QUEUED` |
| `NOT_CROSSED_NO_EXECUTION` | `ABORT_NO_EXECUTION` | `ABORT_NO_EXECUTION` |
| `NOT_CROSSED_PREPARED` | `ABORT_PREPARED` | `ABORT_PREPARED` |
| `CROSSED_ACTIVE` | `WAIT_CROSSED` | `REQUEST_CANCEL` |
| `KNOWN_TERMINAL` | `RETAIN_TERMINAL` | `RETAIN_TERMINAL` |
| `RECONCILIATION_EVIDENCE` | `WAIT_RECONCILIATION` | `WAIT_RECONCILIATION` |

Historical Runs that are not a Subtask's `current_run_id` are evidence only and receive no action.
After actions, any crossed sibling (`WAIT_CROSSED` or `REQUEST_CANCEL`) yields `WAIT_ACTIVE`;
otherwise any reconciliation-required
sibling/evidence yields `WAIT_RECONCILIATION`. With neither, the completion matches the effective
drain target. `CONTINUE_SUCCESS` is allowed only without a drain. The planner validates exact
execution/Attempt IDs and fences carried by each action, is deterministic for equal immutable
input, never mutates the aggregate, never reads a clock, repository, gate, or adapter, and has an
AST-enforced zero production call-site until d2.

###### c.2d2 — transaction-local barrier applier

Add `CoordinatedRuntimeBarrierApplier.apply_in_uow(uow, *, aggregate, plan, now,
cancel_deadline_window, defer_task_save=False) -> CoordinatedBarrierApplication`. The caller must already hold the aggregate
returned by the sole b2 locker. This method never opens or commits a UoW, never reacquires a row in
a different order, and never calls an adapter. It first revalidates that every plan identity,
classification, drain version and ordered action still equals the locked aggregate. `now` and
the positive bounded `cancel_deadline_window` are caller policy inputs; `now` is aware UTC and
monotonic against every changed row. A cancellation deadline is derived from the immutable active
drain `created_at + cancel_deadline_window`, never from a retry's current clock, so replay produces
the same lifecycle intent bytes.

The applier creates/reuses/retargets at most one active drain. Stable drain identity is
`uuid5(NAMESPACE_URL, f"coordination-runtime-drain:{tenant_id}:{task_id}")`; replay must accept the
existing exact row and reject a colliding row. It then applies actions in sorted Run UUID order:

- `RELEASE_QUEUED`: cancel the old queued Run as undispatched evidence, call
  `Subtask.release_never_dispatched_run`, and create no Attempt/execution/lifecycle row;
- `ABORT_NO_EXECUTION`: release budget/quota once, cancel and fence the current Attempt and Run,
  then release the Subtask binding; reject any execution discovered in the locked aggregate;
- `ABORT_PREPARED`: first call the exact-owner/fence `RuntimeExecution.abort_before_dispatch`, save
  it and emit one deterministic `agentmesh.runtime.dispatch.aborted` Outbox event, then perform the
  same Attempt/Run/accounting/Subtask release; create no lifecycle operation;
- `REQUEST_CANCEL`: create/reuse exactly one operation
  `runtime-cancel:{execution_id}:v1` with the plan deadline. Same intent bytes replay; a different
  operation/deadline is a conflict. Do not change the active Attempt, Run, or Subtask;
- all retain/wait actions make no business mutation.

Budget and quota releases use the existing controllers only after the complete aggregate is
locked. They occur in deterministic Attempt UUID order and are idempotent. The abort audit event
uses a deterministic message identity derived from drain ID plus execution ID so command replay
cannot publish twice. Any repository, accounting, Outbox, or lifecycle failure rolls the caller's
whole transaction back. The result returns the effective drain projection, changed entity IDs,
stable lifecycle operation IDs, completion, and `made_progress`; it never claims provider stop for
`REQUEST_CANCEL`.

`defer_task_save` is transaction ownership, not barrier policy. Its default is `False`, preserving
the standalone d2 contract: when sibling accounting changes the Task, d2 saves it before return.
The d3 aggregate command passes `True` because target accounting, sibling accounting, and drain
completion can all change the same Task in one UoW. In that mode d2 still includes the Task ID in
`changed_ids` but does not save it; d3 must persist the final combined Task projection exactly once
before scheduling or commit. No adapter, Worker, public API, or partial aggregate caller may defer
that save.

d2 has no public command and no production caller except d3 in the next commit. Unit tests use
immutable before/after snapshots and injected failures. Real PostgreSQL tests prove queued/no-
execution/PREPARED release, exactly-once accounting, stable cancel intent, full rollback, replay,
and both sides of the c.2c CAS race. An AST guard proves zero adapter calls and zero calls from the
Worker, scheduler, or public services.

###### c.2d3 — aggregate known-terminal command

Add `CoordinatedRuntimeConvergenceService.apply_known_terminal(...)` with ID-only authority:

```text
apply_known_terminal(
    *, tenant_id, task_id, run_id, attempt_id, fencing_token,
       runtime_execution_id, observation, received_at, causation_id
) -> CoordinatedKnownTerminalResult

CoordinatedKnownTerminalResult.kind =
    APPLIED | REPLAY | DRAINING_ACTIVE | DRAINING_RECONCILIATION
```

The command opens one UoW and performs the b2 aggregate lock as its first database operation. It
selects the exact current managed Executor Run/Subtask/latest running Attempt and exact crossed
RuntimeExecution/Assignment snapshot. It validates the canonical `RuntimeObservation` with the
shared terminal contract, requires a known terminal phase, exact assignment/execution identity,
and a monotonic `received_at`. The Task may be `RUNNING`, or `RECONCILIATION_REQUIRED` only when an
active drain proves this target is a late crossed sibling; every other Task state is rejected.
c.2d accepts the existing deterministic-runtime accounting shape;
non-empty or malformed usage, governed-action requests, unresolved waits, or unsupported Artifact
payloads fail before writes and are widened only with the c.2f parity qualification.

Within the same UoW it records one immutable observation evidence row, advances the execution to
the known terminal phase, and settles/releases only the target Attempt and quota. It then makes the
target Run/Attempt/Subtask terminal. A success stores the bounded mapping output; failure, timeout,
or unrequested cancellation stores only the safe error. An exact evidence replay returns `REPLAY`
only when Runtime and local business projections already match; same ID with different digest,
same digest with different ID, stale owner/fence, a second terminal conclusion, or incomplete
local convergence is a conflict. c.2d does not open an integrity incident; that remains c.2e.

Before its first write, the command obtains the d1 plan from the locked pre-observation aggregate.
After local mutation, it invokes d2 with that same aggregate/plan in the same UoW; d2 revalidates
the untouched sibling projections and the triggering identities without changing lock order:

- ordinary success with no drain invokes the existing coordinated scheduler in the same UoW and
  inherits the persisted cohort into successors/Supervisor; replay schedules nothing twice;
- a failure/unrequested cancellation starts or retains a `FAILED` drain and applies sibling
  actions. A provider `CANCELED` with a matching intent and no pre-existing drain is reserved for
  the c.2f cancellation entry contract and is rejected in c.2d;
- `WAIT_ACTIVE`/`WAIT_RECONCILIATION` commits local evidence and returns the matching draining kind
  while the Task remains nonterminal and no successor/Supervisor is scheduled;
- when no active/reconciliation sibling remains, complete the drain and apply its immutable target.
  c.2d applies `FAILED`; it may resume `RUNNING` only for a pre-existing future c.2e hold. Applying
  `WAITING_APPROVAL` or `CANCELED` remains disabled until c.2f and fails before mutation if somehow
  requested without the later entry contract.

Inbox consumption and Worker wiring remain c.2f, so d3 has zero production callers. Unit matrices
cover all four phases, canceled-with/without-intent, success scheduling, first-failure retention,
zero/one/multiple siblings, every b1 boundary, replay/conflict, and failure rollback. Real
PostgreSQL tests cover parallel success, failure with queued/absent/PREPARED/crossed siblings,
late crossed success/failure, concurrent first failures, one stable drain/cancel intent, scheduler
exactly once, accounting/quota exactly once, and atomic evidence/business/Outbox rollback. Full
non-PostgreSQL and PostgreSQL suites, Ruff, architecture tests, `alembic check`, and
`git diff --check` close c.2d.

The d3 implementation contract is fixed as follows so the command does not have to infer policy
from the legacy Worker path:

1. Put the result vocabulary and service in
   `agentmesh.application.coordinated_runtime_convergence`. The constructor receives a UoW
   factory, the already configured `CoordinatedScheduler`, the bounded cancellation window, and
   optional locker/applier collaborators. `tenant_id` remains a command argument; no process-wide
   tenant or rollout decision is captured by the service. The persisted cohort is authority. The
   service may enforce the shared managed-Runtime dependency, but must not read the coordinated
   cutover gate to reinterpret an already admitted Run.
2. Validate scalar types, aware-UTC `received_at`, `causation_id`, and canonical SDK observation
   shape before opening the UoW. After entering it, the first repository call is exactly
   `CoordinatedRuntimeAggregateLocker.lock(tenant_id, task_id)`. Only then resolve the supplied
   Run, Subtask, latest Attempt, execution, and Assignment snapshot from tenant/Task/Run/Attempt/
   execution identity plus the fence. The command never accepts entity objects from its caller.
3. Validate the known-terminal contract against the locked Assignment. For c.2d, reject non-empty
   usage, Artifact refs, governed-action requests, waits, a non-mapping success output, and any
   output on a non-success. Normalize `received_at` to UTC and require it not to precede
   `observation.observed_at` or any changed target row. Derive cancellation authority only from the
   exact stable lifecycle row already in the aggregate. Derive safe failure text only from the
   bounded provider error code, falling back to `runtime.failed`, `runtime.timed_out`, or
   `runtime.unrequested_cancellation`; never persist the provider message as business state.
4. Still before a write, query observation evidence for the target execution and classify the
   command. A unique prior `APPLIED` row with the same observation ID and digest is an exact replay
   only if assignment, phase, sequence, terminal Runtime projection, target Attempt/Run/Subtask,
   accounting release/settlement, active-or-complete drain, and Task projection all match the
   canonical result. Return `REPLAY` without adding evidence, applying the barrier, releasing
   accounting, or scheduling. Same ID/different digest, same digest/different ID, more than one
   accepted terminal row, a different terminal phase, or partial local convergence is a conflict.
   Prior `DUPLICATE`, `STALE_OWNER`, `GAP`, or `CONFLICT` evidence is never promoted to replay.
5. On a first delivery, create the d1 barrier plan from the untouched locked aggregate before the
   first write. Preflight budget accounting on copies of Task/Attempt. Success uses the existing
   empty-usage conservative settlement; failure, timeout, and cancellation use release. Any budget
   result that would require the not-yet-enabled waiting-approval path is rejected before writes.
   Quota is released only after the same preflight succeeds.
6. Persist exactly one `APPLIED` observation and its Runtime terminal transition through the shared
   registry transaction-local primitive. Any registry outcome other than `APPLIED` is a conflict
   and rolls back. Then apply target accounting and the following local terminal table, saving each
   entity once after its final in-transaction state:

| Provider phase | persisted Runtime phase | Attempt / Run / Subtask |
|---|---|---|
| `SUCCEEDED` | `SUCCEEDED` | succeed / succeed(output) / complete(output) |
| `FAILED` | `FAILED` | fail(safe code) / fail(safe code) / fail(safe code) |
| `TIMED_OUT` | `TIMED_OUT` | fail(`runtime.timed_out` or safe code) on all three |
| `CANCELED`, no stable intent | `CANCELED` | fail(`runtime.unrequested_cancellation`) on all three |
| `CANCELED`, stable intent and pre-existing drain | `CANCELED` | cancel / cancel / cancel |

   A requested cancellation without a pre-existing drain remains rejected because the c.2f entry
   contract is the only authority allowed to create the initial `CANCELED` drain.
7. Invoke d2 with the same pre-observation aggregate and d1 plan after the target mutation. d2 may
   inspect only the untouched sibling projections and triggering identities; it must not reload the
   target. d3 passes `defer_task_save=True`, combines target-accounting, sibling-accounting, and
   completion changes, and persists the final Task projection once. Interpret its completion
   exactly once:

| Completion | Task/drain action | Result kind |
|---|---|---|
| `CONTINUE_SUCCESS` | no drain; call `CoordinatedScheduler.schedule` in the same UoW after the target save, with `received_at` and `causation_id` | `APPLIED` |
| `WAIT_ACTIVE` | retain active drain and keep Task `RUNNING` (or its existing reconciliation hold) | `DRAINING_ACTIVE` |
| `WAIT_RECONCILIATION` | retain active drain and call `Task.require_coordination_runtime_reconciliation` when Task was `RUNNING` | `DRAINING_RECONCILIATION` |
| `APPLY_FAILED` | complete/save drain, then use the ordinary or reconciliation-specific Task failure transition according to the locked Task pre-state | `APPLIED` |
| `APPLY_RUNNING` | complete/save a pre-existing drain and resume only an exact `RECONCILIATION_REQUIRED` Task | `APPLIED` |
| `APPLY_WAITING_APPROVAL` / `APPLY_CANCELED` | reject before the observation write in c.2d | none |

8. Commit once, after Runtime evidence, target business state, sibling barrier effects, drain/Task
   completion, scheduler RunRequested Outbox, and accounting are all staged. The result is a frozen
   projection containing the closed kind, the tenant/Task/Run/Attempt/execution identities,
   observation ID/digest,
   resulting Task/Run/Subtask status, optional drain ID/target, and UUID-sorted newly scheduled Run
   IDs. It contains no provider output beyond the already bounded business output.
9. Tests must prove the operation log begins with the aggregate locker, exact replay performs zero
   saves/adds/Outbox writes, and every injected failure point rolls back the observation as well as
   all business state. AST guards scan production modules and fail on adapter imports/calls, on a
   second UoW/commit inside transaction-local helpers, and on any d3 caller outside its own module
   until c.2f.

The slices remain intentionally separate: c.2b2 adds only the fixed aggregate lock/helper and uses
the classifier without applying drain behavior; c.2c adds the closed gate/cohort candidate and
prepare/dispatch primitives without effective admission; c.2d adds known-terminal finalization and
sibling draining; c.2e adds
unknown-outcome parking and privileged reconciliation; c.2f closes cancellation, pause, budget,
lifecycle, concurrency, legacy-parity qualification, and only then activates admission/Worker
wiring. A slice must not pull behavior from a later slice without amending this contract first.

- add coordinated cutover gate and startup guard;
- inherit one cohort into Subtask/Supervisor Runs;
- implement mode-aware parking, safe queued release, active-sibling handling, and convergence;
- extend privileged reconciliation to Subtask and Supervisor Runs;
- fan out the A4.2a.1 lifecycle protocol through the coordinated drain/barrier;
- keep server gate disabled.

### A4.2d — parity qualification

- run the same deterministic scenario on legacy and managed authorities;
- compare Task, Run, Subtask, review, budget, quota, Artifact, Memory, and audit semantics;
- cover reviewed accept/revise/limit/deadline and coordinated parallel join/failure/unknown/cancel;
- run both LangGraph and generic subprocess conformance where capabilities permit;
- publish a machine-readable parity report;
- do not enable production admission.

Each slice is a separate PR unless a reader-only compatibility PR must be deployed before its
writer. Every PR states authority changes, migration floor, rollback target, gate state, crash
windows, and test evidence.

## 13. Required tests

### Unit/domain

- gate dependencies/defaults/startup restrictions;
- cohort first admission, inheritance, gate flip, mixed-authority rejection, Runtime revocation;
- work-item vectors for all roles and revisions;
- Assignment snapshot canonical round-trip, same-bytes replay, changed-bytes conflict, corruption,
  and replacement without a second Memory retrieval;
- terminal contract contradictions for every phase;
- reviewed executor/reviewer success, revision, deadlines, limits, budget waits;
- Subtask park/reconcile/release methods and ordinary-transition rejection;
- convergence barrier with zero/one/multiple unresolved siblings.

### Real PostgreSQL

- concurrent first coordinated scheduling produces one cohort and no duplicate Runs;
- every managed Run has a distinct execution intent and the pinned cohort Runtime Version;
- reviewer/revision/Supervisor creation and Outbox commit atomically with predecessor success;
- exact replay is stable and changed idempotency input conflicts;
- stale Attempt/fence has zero business, accounting, evidence, Memory, and Outbox side effects;
- unknown outcome parks without redispatch;
- two parallel unknown Subtasks reconcile in either order and schedule once;
- one unknown plus one active success waits at the barrier;
- one unknown plus one active failure converges to failure only after uncertainty closes;
- queued non-dispatched siblings are safely released and recreated once;
- exact handle canonical bind/reconstruct and crash after dispatch but before handle bind;
- cancel intent before handle, eventual handle bind, and deadline-without-handle parking;
- after a persisted receipt, replay makes no provider call; response-loss replay may call again with
  the same operation ID but creates one provider cancellation effect;
- lifecycle deadline atomically expires the operation and parks unknown outcome;
- lifecycle backoff is deterministic and deadline-clamped; concurrent due claims make one call,
  response-loss retry reuses the same operation ID, and every invalid receipt phase/accepted pair
  fails closed without changing business state;
- forced conflict plus synthetic unknown rolls back business/accounting/evidence/Inbox/Outbox as one
  UoW; a late second terminal opens one integrity incident without rewriting business state;
- incident exact replay has one row/event; a second conflict digest has a second row; tenant/RBAC,
  idempotent acknowledgement, monotonic escalation, and safe projection are enforced;
- canceled-Task Runtime-only reconciliation covers all four known conclusions, preserves every
  business/accounting/Memory/Artifact projection, quarantines success output, and conflicts on a
  competing conclusion;
- reconciled failure plus an active sibling creates/reuses one cancel intent and drains before Task
  failure; concurrent failures retain one drain target and one operation per sibling;
- a budget `WAITING_APPROVAL` drain is a stopping target and creates the same one-per-execution
  cancellation intents as failure/cancellation drains;
- PostgreSQL barriers force both sides of the pre-dispatch race: drain-before-prepare, drain after
  `PREPARED` but before boundary CAS, and boundary-CAS-before-drain. The first two make zero adapter
  calls, release reservation/quota once, consume duplicate wakeups without creating another
  execution, and preserve a `READY` undispatched Subtask; the last records `DISPATCHING`, invokes
  the adapter once with the stable key, and creates one stable CANCEL intent without falsely
  canceling the active Attempt;
- crash after committed dispatch-boundary CAS and before adapter invocation parks unknown without
  redispatch; stale Attempt/fence or any active drain makes the CAS fail with zero provider calls;
- accounting and quota are settled/released exactly once;
- Memory failure rolls back terminal convergence; research failure is post-commit;
- DEPRECATED cohort dispatch succeeds, revoke races fail before provider contact, and gate flips do
  not mix authority;
- expand migration downgrade succeeds before writer values and rejects after snapshot/status rows
  without data loss or cross-tenant cleanup.
- lifecycle expansion backfills existing rows without scheduling them; old readers/writers tolerate
  the columns; default-only rows downgrade; any due time, claim triple, error, or nonzero attempt
  count makes downgrade refuse without data loss.

### E2E/chaos precursor

- reviewed accept and one-revision paths under managed authority;
- coordinated parallel DAG and Supervisor join under managed authority;
- Worker death before dispatch, after dispatch marker, and after provider response;
- provider response loss, conflict before commit, late conflicting terminal after commit, and late
  success after cancellation;
- UI/API projections require no framework-specific branch.

## 14. Exit criteria

A4.2 is complete only when:

- newly admitted test-only reviewed and coordinated Tasks can use managed authority end to end;
- every Run in a multi-Run Task has one immutable authority cohort and a stable execution intent;
- canonical Assignment input equals executed input for every role;
- legacy and managed paths use the same business outcome applier;
- reviewed and coordinated unknown outcomes converge without blind redispatch;
- parallel coordinated uncertainty cannot schedule successors early or duplicate a Run;
- cancellation/late-result handling does not claim an external execution stopped without evidence;
- free CI, unit coverage, real PostgreSQL, Compose E2E, dependency review, and CodeQL pass;
- both new gates remain disabled on the public server;
- rollback/schema floors and remaining A4.3 production limitations are documented.
