# Revenue and financial governance

Status: Proposed

## Outcome

AgentMesh should help a Virtual Company discover, execute, and measure revenue-producing work while
keeping capital allocation, commercial commitments, payments, and regulated actions under explicit
human and Policy control.

The platform may optimize a business process. It must not promise profit, invent financial results,
or treat an Agent estimate as cash.

## Economic evidence model

The system distinguishes:

| Concept | Meaning |
|---|---|
| Revenue opportunity | A qualified possibility, not booked revenue |
| Offer value | Proposed commercial value, not a customer commitment |
| Contracted revenue | Supported by an accepted contract/order reference |
| Invoiced revenue | Invoice issued in an external system of record |
| Collected cash | Reconciled payment evidence |
| Reserved cost | Budget held for planned execution |
| Settled cost | Verified model, Tool, service, or external expense |
| Estimated margin | Forecast using labelled assumptions |
| Verified margin | Reconciled revenue minus settled attributable cost |

Every dashboard and KPI preserves this classification.

## Revenue loop

```text
Observe market or customer need
  → Propose opportunity
  → Qualify evidence
  → Design offer
  → Price and cost review
  → Risk and owner approval
  → Deliver bounded work
  → Customer acceptance
  → Invoice request
  → External reconciliation
  → Margin review
  → Candidate organizational learning
```

AgentMesh coordinates the loop and stores evidence. CRM, contract, accounting, banking, payment,
and tax systems remain external systems of record.

## Commercial objects

### Opportunity

```text
Opportunity
- id
- customer_id
- source
- problem_statement
- qualification_evidence
- estimated_value
- estimated_probability
- owner_position_id
- stage
- next_review_at
```

Probability is an estimate with provenance and model/version metadata. It is not multiplied into
reported revenue without an explicit forecast view.

### Offer

```text
Offer
- id
- opportunity_id
- product_or_service
- scope
- exclusions
- deliverables
- proposed_price
- estimated_cost
- validity_period
- risk_classification
- status
- approved_version
```

Sending an Offer externally is a governed action. Material changes create a new version and may
require new approval.

### Commercial commitment

```text
CommercialCommitment
- id
- offer_id
- external_contract_ref
- customer_acceptance_ref
- amount
- currency
- obligations
- effective_at
- expires_at
- verification_status
```

AgentMesh stores references and bounded structured obligations, not complete secrets or payment
credentials.

### Invoice and cash evidence

```text
InvoiceEvidence
- id
- commitment_id
- external_invoice_ref
- amount
- currency
- issued_at
- due_at
- status
- source_snapshot_id
```

```text
CashReceiptEvidence
- id
- external_payment_ref
- invoice_id
- amount
- currency
- received_at
- reconciliation_status
- source_snapshot_id
```

Only an authorized accounting/payment adapter can create verified observations.

## Budget model

Existing Task budget admission covers model Token and cost. Company budgeting adds hierarchical
allocation:

```text
Company Budget
  └─ Operating Cycle Allocation
      └─ Department Allocation
          └─ Initiative / Operation Allocation
              └─ Task Reservation and Settlement
```

```text
BudgetAllocation
- id
- parent_allocation_id
- scope_type
- scope_id
- currency
- approved_limit
- reserved_amount
- settled_amount
- period
- policy_version
- status
```

The ledger is append-only. A cached balance may accelerate admission but cannot replace ledger
evidence.

Currency conversion is excluded from the first slice; allocations use one Company default
currency. Imported multi-currency transactions remain separate until an approved exchange-rate
source and accounting policy exist.

## Expense and payment requests

```text
ExpenseRequest
- id
- company_id
- requested_by_position_id
- purpose
- vendor_ref
- amount
- currency
- budget_allocation_id
- evidence_refs
- risk_tier
- approval_id
- status
```

```text
PaymentRequest
- id
- expense_request_id
- destination_reference
- amount
- currency
- idempotency_key
- approval_id
- external_operation_id
- outcome_status
```

Destination references are opaque identifiers managed by an external payment adapter. Raw bank or
card credentials never enter Agent context, Memory, Artifact, or audit export.

## Risk tiers

Recommended default:

| Tier | Examples | Default behavior |
|---|---|---|
| `R0_READ` | Read metrics, invoices, budgets | Allowed by scoped credential |
| `R1_INTERNAL` | Create forecast, draft offer, expense proposal | Automatic within role |
| `R2_EXTERNAL_LOW` | Send approved report, update CRM stage | Policy + bounded Permit |
| `R3_COMMITMENT` | Send price, accept terms, buy ads, issue invoice | Human approval |
| `R4_PAYMENT` | Transfer funds, refund, purchase, payroll | Strong multi-stage approval |
| `R5_PROHIBITED` | Autonomous trading, borrowing, opening accounts | Fail closed |

Companies may make policies stricter, not silently weaker than platform hard prohibitions.

## Approval rules

Financial actions may require:

- amount threshold;
- budget availability;
- requester/approver separation of duties;
- Finance Controller review;
- Risk Officer review;
- owner approval;
- quorum;
- destination allowlist;
- recent credential verification;
- obligation fulfilment;
- maximum Permit validity.

The Agent that proposes an Expense or Offer cannot satisfy an independent approval stage using a
different role label bound to the same prohibited identity when separation of duties is required.

## External action semantics

Every external financial or commercial write uses:

- canonical ActionIntent digest;
- one-time Permit;
- stable idempotency key;
- exact credential binding;
- request/response size bounds;
- timeout and retry policy;
- explicit `UNKNOWN` outcome;
- reconciliation evidence before retry.

An unknown payment or invoice outcome never triggers blind replay. The corresponding budget amount
remains reserved until an operator or adapter proves delivered or not delivered.

## Revenue attribution

AgentMesh may attribute revenue and cost for operational analysis:

```text
AttributionRecord
- id
- evidence_type
- evidence_id
- company_id
- department_id
- initiative_id
- operation_id
- task_id
- amount
- currency
- attribution_method
- verification_status
```

Attribution is not general-ledger accounting. Reports label direct, allocated, estimated, and
unattributed values.

Examples:

- model and Tool cost settles directly to a Task;
- a deliverable invoice links to an Initiative;
- shared software subscription cost is allocated by approved policy;
- employee “performance revenue” is not inferred from correlation alone.

## Finance Agent

Recommended Finance Controller responsibilities:

- import and reconcile evidence;
- detect budget exceptions;
- prepare cash-flow and margin reports;
- propose invoices and expense classifications;
- verify that financial KPIs use correct evidence classes;
- escalate overdue or unknown outcomes.

The Finance Agent cannot:

- modify source transactions;
- approve its own high-risk request;
- reveal payment credentials;
- make tax or legal representations without an approved specialized process;
- initiate prohibited financial activity.

## Owner dashboard

The management surface separates:

- pipeline value;
- approved offers;
- contracted value;
- invoices;
- collected cash;
- reserved and settled cost;
- estimated and verified margin;
- overdue obligations;
- unknown external outcomes;
- approval queue.

Every amount is clickable to its evidence and classification. Office displays may show trend and
exception indicators but not sensitive customer or account details.

## Revenue-seeking autonomy

An Agent may autonomously:

- research a market;
- identify and qualify candidate opportunities;
- propose products, offers, prices, and experiments;
- create internal drafts and forecasts;
- execute pre-approved low-risk recurring operations;
- recommend budget changes.

It may not autonomously:

- guarantee returns;
- spend outside an approved allocation;
- materially change price or terms after approval;
- sign contracts;
- open financial accounts;
- borrow money;
- trade financial assets;
- conceal losses, uncertainty, or failed experiments.

## Feature gates

- `company_finance_read`
- `financial_governance`
- `commercial_actions`
- `payment_actions`

Read-only finance may ship before any external write adapter. Payment actions remain separately
disabled even when other financial features are enabled.

## Delivery slices

### Slice 1 — internal economics

- hierarchical budget allocations;
- Task cost reservation/settlement linkage;
- Opportunity, Offer, ExpenseRequest, and attribution objects;
- estimated versus verified reporting;
- deterministic fixtures and owner dashboard.

### Slice 2 — accounting evidence

- immutable Invoice and Cash Receipt evidence;
- confined read-only accounting adapter;
- reconciliation and freshness status;
- revenue/cost/margin evidence bundle;
- no external writes.

### Slice 3 — governed commercial writes

- send approved Offer or issue invoice through a bounded adapter;
- staged approval and one-time Permit;
- idempotency and unknown-outcome reconciliation;
- separation-of-duties tests.

### Slice 4 — high-risk payment boundary

- PaymentRequest lifecycle and destination references;
- multi-stage approval;
- provider sandbox adapter only;
- failure injection and reconciliation certification;
- production adapters remain external and off by default.

## Acceptance criteria

- Estimated pipeline cannot appear as verified revenue.
- Collected cash requires reconciled external evidence.
- Every Task cost settles to an allocation or remains explicitly unattributed.
- Budget admission is atomic under concurrent Tasks.
- A proposer cannot satisfy a prohibited approval stage.
- Unknown financial writes cannot be automatically repeated.
- No secret payment credential appears in database business objects, Memory, logs, Artifacts,
  Console exports, or Office projections.
- Disabling financial gates leaves the existing Task cost ledger unchanged.
- CI exercises all slices available in-repository without real money or external financial calls.

## Non-goals

- replacing accounting, banking, tax, payroll, or legal systems;
- autonomous investment, securities trading, lending, or borrowing;
- guaranteeing profit;
- producing regulated financial advice;
- accepting model estimates as booked revenue;
- allowing a digital employee to hold unrestricted company funds.
