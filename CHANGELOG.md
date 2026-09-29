# Changelog

All notable changes to AgentMesh are documented here. Versions follow Semantic Versioning for
release tags and PEP 440 for the Python package.

## Unreleased

No changes yet.

## 0.2.0-alpha.1 — 2026-09-29

Second public Alpha: a guided, bilingual Console over the durable single-team control plane.

### Added

- Authenticated, encrypted OpenAI/DeepSeek model connections, version-bound employee execution,
  explicit connection testing, and reviewed PostgreSQL employee memory setup.
- Guided Direct/Reviewed/Coordinated task creation, published-employee assignment, plan review,
  light minimalist Console, and a real-DeepSeek customer-feedback walkthrough with screenshots.
- Framework-neutral Runtime SDK and LangGraph/subprocess conformance, with managed Direct,
  Reviewed, and Coordinated qualification behind default-off gates. The A4.2d report records
  documented failure, budget, cancellation, and unknown-outcome differences.
- Virtual Company goals, Operations, typed Business Objects, reviewed memory, internal finance,
  and declarative Packs, including an offline Market Intelligence evidence chain.
- Trusted Runtime Extensions, locked third-party wheel admission and installation receipts, plus
  a separate Extension Starter. Music Studio remains a deterministic keyless scenario demo.

### Changed

- The focused Console is the primary interface. The game-style Office renderers were retired;
  `/world` and `/world-3d` now redirect to `/`. Company and employee records remain intact.
- Documentation now starts with concise bilingual READMEs and a task-oriented guide map; obsolete
  Office/Bootstrap plans were removed from the current tree.

### Release boundary

- The public demo uses the deterministic executor. A separate private DeepSeek stack completed
  Direct and three-employee Coordinated acceptance; live OpenAI and production provider operation
  are not qualified by that result.
- Supported for evaluation and non-critical single-team deployments, not production HA or
  multi-tenant use. Managed-runtime cutover remains disabled by default.

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
