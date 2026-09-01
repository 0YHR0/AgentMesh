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

- add Subtask reconciliation enum reader support;
- expand the PostgreSQL constraint;
- add schema-floor upgrade/downgrade tests;
- expose no writer transition or gate.

### A4.2c.2 — coordinated writer and convergence barrier

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
