# Financial Governance Implementation

Status: Implemented baseline  
Feature gates: `company_finance_read`, `financial_governance`

## Boundary

This module is AgentMesh's internal economic evidence and capital-allocation control plane. It is
not a general ledger, accounting package, bank, payment processor, tax engine, or promise of
profit. External commercial writes and movement of money remain disabled.

## Runtime model

- `BudgetAllocation` forms a single-currency Company → cycle/unit → initiative/operation tree.
- `BudgetLedgerEntry` is append-only and records reserve, release, and settlement operations in
  integer micros.
- Every leaf operation is mirrored to each ancestor while the lineage is locked. This makes
  admission atomic across sibling allocations instead of merely checking a stale parent balance.
- `operation_key` is unique per allocation. An exact replay returns the original entry; reuse with
  different semantics fails closed.
- `EconomicEvidence` keeps opportunity, offer, contract, invoice, cash, and cost classes separate.
  Contracted/invoiced/cash/cost facts require verified external reference plus snapshot digest.
- `ExpenseRequest` is a proposal and approval record. An approved request reserves its allocation,
  and the requester cannot approve the same request.

Balances are derived from the append-only ledger. Cached counters are intentionally absent from
this baseline.

## API

All routes are under `/api/v1/companies/{company_id}/finance`:

- create/list/close allocations;
- reserve, release, or settle an allocation;
- append/list economic evidence;
- propose/list/review expenses;
- retrieve the evidence-classified owner dashboard.

Read routes require `company_finance_read`; mutations additionally require
`financial_governance`. Both gates are explicit opt-in and are outside the `full` profile.

## Safety invariants

- all amounts are positive integer micros with one Company default currency;
- estimated pipeline and offers never become verified revenue;
- collected cash and settled cost require reconciled-source evidence;
- a closed allocation cannot admit new reservations;
- active reservations prevent allocation closure;
- settlements/releases cannot exceed outstanding reservation on any ancestor;
- self-approval is rejected;
- no destination, bank, card, payment credential, or unrestricted payment action exists here.

## Deferred

Task-cost automatic linkage, read-only accounting connectors, commercial write adapters,
unknown-outcome reconciliation, and payment requests remain separately gated future increments.
Production payment adapters are intentionally outside the repository baseline.
