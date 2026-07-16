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
| [0002](0002-separate-business-execution-and-telemetry-state.md) | Proposed | 分离业务、执行 Checkpoint 和遥测状态 |
| [0003](0003-transactional-outbox-and-redis-streams.md) | Proposed | 使用 Transactional Outbox 和 Redis Streams 至少一次投递 |
| [0004](0004-protocol-anti-corruption-layers.md) | Proposed | A2A/MCP 通过 anti-corruption adapter 接入 |
| [0005](0005-modular-monolith-and-worker-deployment.md) | Proposed | 模块化控制面与独立 Worker/Event Relay 起步 |
| [0006](0006-start-minimal-and-enable-capabilities-with-feature-gates.md) | Accepted | 默认最小运行，通过 Feature Gate 显式开启高级能力 |
