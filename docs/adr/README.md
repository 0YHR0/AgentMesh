# Architecture Decision Records

ADR 用于记录跨模块、影响长期演进或难以逆转的决定。已经接受的 ADR 不应被直接修改结论；如需改变，应创建新的 ADR 并标记旧 ADR 为 Superseded。

## Format

每份 ADR 包含：

- Context
- Decision
- Consequences
- Alternatives considered
- Status and date

## Index

| ADR | Status | Decision |
|---|---|---|
| [0001](0001-documentation-first.md) | Accepted | 使用文档先行和分层架构设计 |
| [0006](0006-start-minimal-and-enable-capabilities-with-feature-gates.md) | Accepted | 默认最小运行，通过 Feature Gate 显式开启高级能力 |
| [0007](0007-framework-neutral-agent-control-plane.md) | Accepted | 将 AgentMesh 收敛为框架中立的 Agent Control Plane |

早期未接受的 0002–0005 草案已从当前文档树移除；相关实现边界现在由
[L0/L1 架构概览](../architecture/README.md)、[模块文档](../architecture/modules/README.md)
和当前代码描述。原稿仍可在 Git 历史中查阅，因此编号不会重用。
