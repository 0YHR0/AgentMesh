# Company Packs Implementation

Status: Implemented extended baseline
Feature gate: `company_packs`  
Dependencies: `company_model`, `business_objects`

Company Packs add reusable business semantics without hard-coding an industry into AgentMesh.
A Pack is immutable declarative JSON with a semantic version, content digest, required Feature
Gates, dependencies, and a bounded resource list. Arbitrary Pack-supplied executable code is
rejected by design.

The baseline supports Organization Units, Positions, published Business Object Types, Budget
Allocations, Operating Cycles, Objectives, Key Results, Initiatives, Memory Policies, and Company
Operations. A Template is a Pack composition marker, not a separate runtime. Installation follows:

1. publish a validated Pack;
2. preview exact resources, missing dependencies/features, and content digest;
3. submit the expected digest;
4. validate conflicts and apply every resource plus the installation ledger in one transaction.

An exact install replay returns the original installation. Installing another digest under the
same Pack key requires a future explicit upgrade workflow and fails closed today. Resource
references and the Pack digest remain durable audit evidence.

The built-in Market Intelligence Studio uses two composable Packs:

- the base Template creates organization and typed business semantics without requiring an API key;
- the optional Operations Pack depends on the base digest-pinned installation and creates the
  first governed cycle, objective/KRs, initiative, budget, Memory Policy, and draft recurring
  Operations.

Cycle, Objective, and Initiative activation is part of the explicit owner-authorized Pack
transaction. Recurring Operations intentionally remain `DRAFT`, so installing a Pack cannot
silently dispatch Tasks. External writes remain disabled.

Agent Appointments, connectors, policy bundles, Office assets, upgrade/downgrade, signatures, and
a remote Pack registry remain later extensions.
