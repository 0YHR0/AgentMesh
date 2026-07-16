# ADR 0003: Transactional Outbox with Redis Streams delivery

Status: Proposed
Date: 2026-07-16

## Context

业务状态提交后需要唤醒 Worker、更新投影和触发通知。PostgreSQL commit 与消息 broker publish 不能原子完成；进程可能在任一窗口崩溃。

## Decision

- 领域状态与 Outbox Event 在同一 PostgreSQL 事务写入。
- 独立 Event Relay 使用 SKIP LOCKED 批量发布到 Redis Streams。
- 发布和消费均采用至少一次语义；消费者使用持久化 Inbox 去重。
- Redis 负责投递、唤醒和短期 backlog，不是业务账本。
- Publisher/Consumer Port 隔离 Redis，未来可按证据迁移 NATS JetStream。

## Consequences

- 数据库提交后不会因 broker 短暂故障丢失业务事件。
- 重复消息是正常情况，所有消费者必须幂等。
- 需要运营 Outbox lag、consumer pending、dead letter 和 replay。
- Redis 全量丢失后可从 PostgreSQL 生成必要 recovery wakeup，但可能增加恢复时延。

## Alternatives considered

- 业务代码 commit 后直接 publish：拒绝，存在永久丢消息窗口。
- 分布式事务/2PC：拒绝，外部系统与云 broker支持有限且运维复杂。
- 只轮询业务表：可作为 recovery fallback，但不能满足低延迟、解耦和多消费者需求。
- Kafka 首发：当前规模和运维成本不匹配，未来吞吐/保留需求明确后再评估。
