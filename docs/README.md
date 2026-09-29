# Documentation

[English](README.md) | [简体中文](README.zh-CN.md) | [Project README](../README.md)

**Start with a guide, not a design document.** The tutorials describe the current product;
architecture documents and proposals explain implementation or possible future work.
For the exact shipped boundary, use [implementation status](implementation-status.md).

| Your goal | Start here | Then explore |
| --- | --- | --- |
| Try AgentMesh without a key | [Five-minute guide](getting-started.md) | [Customer-feedback task with real-model screenshots](scenarios/customer-feedback-deepseek.md) |
| Configure employees, models, or memory | [Model and memory setup](model-and-memory-setup.md) | [SSH-only credential administration](operations/model-connections-ssh.md) |
| Use a business scenario | [Customer-feedback walkthrough](scenarios/customer-feedback-deepseek.md) | [Market-research scenario](scenarios/market-research.md) |
| Deploy or operate AgentMesh | [Administrator best practices](best-practices.md) | [SLO and restore](operations/slo-and-restore.md), [runtime rollback](operations/runtime-direct-cutover-rollback.md) |
| Build an extension | [Runtime Extension Protocol](architecture/modules/runtime-extension-protocol.md) | [Extension Starter](https://github.com/0YHR0/AgentMesh-Extension-Starter), [formal module contracts](architecture/modules/formal/README.md) |

## Reference and project state

- **What is implemented:** [Implementation status](implementation-status.md),
  [v1 scope](v1-completion-scope.md), [changelog](../CHANGELOG.md), and
  [release notes](releases/v0.1.0-alpha.1.md).
- **Configuration and APIs:** [Feature gates](architecture/modules/feature-gates.md),
  [Control API design](architecture/modules/formal/control-api.md), and the running service's `/docs` OpenAPI UI.
- **Integration boundaries:** [MCP Registry](architecture/modules/governed-mcp-registry-implementation.md),
  [A2A delegation](architecture/modules/a2a-outbound-delegation-implementation.md), and
  [Agent memory](architecture/modules/organizational-memory-implementation.md).
- **Architecture:** [L0 design](architecture/L0-system-design.md),
  [L1 design](architecture/L1-design-plan.md),
  [L2 module index](architecture/modules/README.md),
  [ADRs](adr/README.md), and [glossary](glossary.md).
- **Planning:** [Roadmap](roadmap.md) and [proposals](proposals/README.md).

The implementation documents are technical references, not setup instructions. Proposals describe
possible future work, not shipped behavior; check implementation status before relying on a
feature. The L0/L1 summaries point to current ownership; accepted ADRs and running code define
the detailed behavior.
