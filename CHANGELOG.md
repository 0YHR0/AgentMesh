# Changelog

All notable changes to AgentMesh are documented here. Versions follow Semantic Versioning for
release tags and PEP 440 for the Python package.

## Unreleased

### Added

- Accepted framework-neutral Agent Control Plane design baseline: Managed Agent Runtime API v0.1,
  Governed Action Protocol v0.1, reliability/chaos qualification, and a slice-by-slice P0
  implementation plan with migrations, rollback gates, and Luna handoff criteria.
- PostgreSQL-backed Office employee placements on an authoritative department grid.
- Cell-snapped employee dragging with occupancy validation and server-derived department moves.
- Rendering-only employee roaming, tablet work motion, foliage sway, and campus light pulse.
- Original role-shaped character presets with walk cycles, breathing, blinking, look-around motion,
  animated accessories, and eased turning.
- Semantic Office behaviors and A* grid navigation for corridor-aware Handoffs, approval travel,
  occupied-cell avoidance, and furniture-reserved cells.
- Tenant-shared PostgreSQL-backed custom Office spaces with bounded create/reset APIs and
  one-time browser-local layout migration.
- Sanitized MCP, A2A, and Policy interaction cards and reduced-motion-aware data-packet projection
  between Agents and governed Office stations.
- Versioned Company Pack SDK definition and Catalog boundary, with Music Studio moved into a
  discoverable scenario package while retaining its existing API and compatibility import.
- Scenario-owned Music Studio runtime, provider adapters, HTTP routes, and workspace assets with
  unchanged public URLs and compatibility re-exports for pre-alpha Python imports.
- Trusted in-process Runtime Extension API v0.1 with entry-point discovery, manifest and collision
  validation, capability-limited service factories, explicit enablement, lifecycle health, and
  fail-closed Music Studio integration.
- Independent AgentMesh Extension Starter reference repository proving two-distribution discovery
  with a deterministic Daily Brief service, API, workspace, health probe, tests, and free CI.
- Fail-closed `extensions.lock` validation before third-party Entry Point imports, locked manifest
  risk declarations, SHA-256 wheel preflight/installer, append-only installation receipts, and
  trust provenance in the Runtime Extension status API.

## 0.1.0-alpha.1 — 2026-07-27

First public Alpha release of the supported single-team v1 baseline.

### Added

- Durable direct, independently reviewed, and coordinated multi-Agent Task execution.
- PostgreSQL system of record, transactional Outbox/Inbox, Redis Streams delivery, fenced
  Attempts, leases, recovery, and LangGraph PostgreSQL checkpoints.
- Versioned Agent Registry, role-bound deterministic/OpenAI runtimes, bounded context assembly,
  usage accounting, budgets, and hierarchical quota admission.
- Governed MCP read/write paths, versioned capability discovery, credentials, Permits, circuit
  breaking, and evidence-backed unknown-outcome reconciliation.
- A2A peer/Card registry, controlled delegation, polling, cancellation, recovery, and operator
  convergence.
- Versioned policy obligations, staged role-constrained quorum approvals, Artifacts, Goal
  Contracts, Plan Patches, Handoffs, audit projections, and shared replay bookmarks.
- Zero-build Web Console with the 20-Agent Mission Map, live interactions, filters, replay,
  inspector, minimap, sanitized export, and deterministic Research Brief Showcase.
- Docker Compose deployment, migrations, backup/restore tooling, SLO runbook, free GitHub CI,
  CodeQL, dependency review, coverage gate, and tag-driven release assets.

### Release boundary

- Supported for evaluation, local development, and non-critical single-team deployments.
- Cross-tenant scheduling/RLS, managed HA/PITR, cloud secret/object-store adapters, A2A
  streaming/push, remote Artifact transfer, and production capacity certification remain
  post-v1 extensions.
