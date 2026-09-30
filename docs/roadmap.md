# Roadmap

Last updated: 2026-09-30 · Status: Alpha

The supported single-team v1 baseline and guided Console are delivered. The
[implementation status](implementation-status.md) lists what actually runs, while the
[v1 scope](v1-completion-scope.md) defines its support boundary. This roadmap lists open tracks
only; it is not a claim that a proposal has shipped.

## Next product validation

- Next priority: opt-in acceptance checks for Coordinated deliverables
  ([#187](https://github.com/0YHR0/AgentMesh/issues/187)). Reuse the existing Reviewed/checking
  primitives where possible; make missing inputs, denominator/unit mismatches and human review
  visible without complicating simple tasks. Complete provider output is not business acceptance.
- Make one real-model customer-feedback workflow easy to repeat from the Console; use the
  [screenshot walkthrough](scenarios/customer-feedback-deepseek.md) as the current acceptance
  example. A private DeepSeek run passed; the public demo remains keyless.
- Validate a distinct governed scenario, such as the
  [software delivery team](proposals/governed-software-delivery-team.md), before expanding the
  platform solely by adding infrastructure features.
- Keep advanced MCP, A2A, company, and memory capabilities optional in the first-run experience.

## Reliability and production work

Production-mode startup now rejects disabled Identity/RBAC and the bundled development database
credential. This prevents two known misconfigurations; it does **not** establish TLS, safe secret
rotation, recoverability, capacity, or a production-ready release.

1. Qualify durable non-LangGraph subprocess restart/reattach and safe admission
   ([#136](https://github.com/0YHR0/AgentMesh/issues/136)).
2. Unify governed external actions beyond today's MCP/A2A paths
   ([#137](https://github.com/0YHR0/AgentMesh/issues/137)).
3. Publish repeatable chaos/correctness evidence before changing managed-runtime cutover defaults
   ([#138](https://github.com/0YHR0/AgentMesh/issues/138)).
4. Address production security, HA/PITR, load, and deployment qualification separately
   ([#160](https://github.com/0YHR0/AgentMesh/issues/160)).

Cross-tenant fair dispatch is [deferred](proposals/cross-tenant-fair-dispatch.md) until a shared
worker pool has real contention evidence. Future scenario or provider plans live under
[current proposals](proposals/README.md); completed phase checklists are retained in Git history.
