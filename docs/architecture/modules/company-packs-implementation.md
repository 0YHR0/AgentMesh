# Company Packs Implementation

Status: Implemented baseline  
Feature gate: `company_packs`  
Dependencies: `company_model`, `business_objects`

Company Packs add reusable business semantics without hard-coding an industry into AgentMesh.
A Pack is immutable declarative JSON with a semantic version, content digest, required Feature
Gates, dependencies, and a bounded resource list. Arbitrary Pack-supplied executable code is
rejected by design.

The baseline supports Organization Units, Positions, and published Business Object Types. A
Template is a Pack composition marker, not a separate runtime. Installation follows:

1. publish a validated Pack;
2. preview exact resources, missing dependencies/features, and content digest;
3. submit the expected digest;
4. validate conflicts and apply every resource plus the installation ledger in one transaction.

An exact install replay returns the original installation. Installing another digest under the
same Pack key requires a future explicit upgrade workflow and fails closed today. Resource
references and the Pack digest remain durable audit evidence.

Memory, Workflow, Connector, Policy, Finance, Office assets, upgrade/downgrade, signatures, and a
remote Pack registry remain later extensions. The first built-in market-intelligence template is
delivered separately on top of this stable contract.
