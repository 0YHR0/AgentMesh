# <Module name> design

Status: Proposed
Owners: TBD
Depends on: <links>

## 1. Problem

这个模块解决什么问题？如果不建设会怎样？

## 2. Responsibilities

- ...

## 3. Non-responsibilities

- ...

## 4. Inputs and outputs

列出命令、查询、事件、Artifact 和协议对象，不必过早绑定 HTTP 路径。

## 5. Dependencies and boundaries

列出同步 Port、异步 Event、外部协议、信任边界和调用方向。

## 6. State ownership

列出权威数据、缓存数据、派生数据和外部引用。

## 7. Component model

使用最小必要的组件图说明内部结构。

## 8. Main flows

覆盖成功路径、重试、取消、超时和人工介入。

## 9. State machine

如果模块拥有有状态实体，定义合法状态和转换守卫。

## 10. Consistency and idempotency

说明事务边界、幂等键、并发控制、事件发布与重复消费。

## 11. Failure model

说明每个依赖失败时的行为、恢复方式、降级和人工处置。

## 12. Security and trust

说明身份、授权、凭证、敏感数据、租户隔离和审计。

## 13. Observability

定义日志、指标、Trace Span、业务事件、成本和质量信号。

## 14. Capacity and limits

说明吞吐、并发、负载、大小限制、保留期和背压。

## 15. Deployment and versioning

说明部署单元、扩缩容、协议/Schema 兼容、升级和回滚边界。

## 16. Testing strategy

定义 unit、contract、integration、failure injection、security 和 performance tests。

## 17. Alternatives

记录考虑过但未采用的主要方案及原因。

## 18. Open questions

- ...

## 19. Acceptance criteria

- ...
