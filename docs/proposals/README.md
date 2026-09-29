# Product and architecture proposals

[Documentation map](../README.md) · [Implementation status](../implementation-status.md)

These are design records, **not a list of currently available features**. Some ideas were
implemented, some only partly, and others were superseded. Use the implementation status and
user guides for current behavior.

## Delivered ideas with remaining follow-up work

- [Guided product onboarding](product-onboarding-vnext.md) — focused Console, model connections,
  and memory setup were delivered in PR #164; see the [current user guide](../getting-started.md).
- [Agent Mission Map](agent-mission-map.md) — durable activity view and replay are available in
  the current Console.
- [Organizational memory](organizational-memory-service.md) — reviewed PostgreSQL memory is
  available; semantic retrieval and richer extraction remain future work.
- [Company operations and business objects](company-operations-and-business-objects.md) — a
  bounded core is implemented; the proposal also contains future integrations.
- [Revenue and financial governance](revenue-and-financial-governance.md) — internal controls are
  implemented; accounting adapters and commercial writes remain proposed.
- [Simple core and clean product experience](simple-core-and-clean-product-experience.md) —
  simplified Console direction is delivered; the document also contains longer-term ideas.

## Current candidates and deferred work

- [Virtual Company operating model](virtual-company-operating-model.md)
- [Employee-first company and extension platform](employee-first-virtual-company-and-extension-platform.md)
- [Music Studio template](music-studio-template.md)
- [Market intelligence studio template](market-intelligence-studio-template.md)
- [Governed software delivery team](governed-software-delivery-team.md)
- [External memory adapters](external-memory-adapters.md)
- [Cross-tenant fair dispatch](cross-tenant-fair-dispatch.md)

Some of these proposals have partial implementations. In particular, a proposal for a scenario
does not mean its live external integrations are configured or shipped. Consult
[implementation status](../implementation-status.md) before planning a deployment.

## Historical, retired interface explorations

The game-style Office surfaces were retired in favor of the focused Console. These documents are
preserved for design history, not for current setup:

- [Office game world](agentmesh-office-game-world.md)
- [Office 2.5D renderer](agentmesh-office-2.5d-renderer.md)
- [Expandable campus](office-primary-surface-and-expandable-campus.md)
