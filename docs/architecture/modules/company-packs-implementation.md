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

An exact install replay returns the original installation. A newer published version under the
same Pack key uses an explicit in-place upgrade workflow:

1. preview a resource-level diff pinned to both the installed and target digests;
2. reject removed resources, newly introduced resources, and changes outside the supported safe
   migration set;
3. allow a changed Business Object Type only when its schema version increases and every current
   object revision still validates against the target schema and lifecycle;
4. update the existing Type and installation in one transaction, preserving resource IDs and
   historical object revisions;
5. append an immutable upgrade audit record and Outbox event.

Replaying an already completed target digest returns the original audit result. The first safe
migration set is intentionally narrow: in-place Business Object Type evolution only. Organization,
Position, Operation, financial, and memory-resource migrations continue to fail closed until each
kind has an explicit migration policy. Downgrade is not supported.

The built-in Market Intelligence Studio uses two composable Packs:

- the base Template creates organization and typed business semantics without requiring an API key;
- the optional Operations Pack depends on the base digest-pinned installation and creates the
  first governed cycle, objective/KRs, initiative, budget, Memory Policy, and draft recurring
  Operations.

Cycle, Objective, and Initiative activation is part of the explicit owner-authorized Pack
transaction. Recurring Operations intentionally remain `DRAFT`, so installing a Pack cannot
silently dispatch Tasks. External writes remain disabled.

The Market Intelligence Studio adds a post-install workforce wizard without making Appointments
part of declarative Pack content. It previews only active Agents whose published default Version
satisfies a Position's required capabilities, persists a bounded set of Appointments in one
transaction, and exposes an atomic staffing preflight before the owner explicitly starts recurring
Operations. This separation keeps Agent identity and Version selection tenant-local and auditable.

Connectors, policy bundles, Office assets, downgrade, signatures, and a remote Pack registry remain
later extensions.
