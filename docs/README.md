# AgentMesh documentation

本文档集按照“先边界、后容器、再组件、最后实现”的顺序演进。

## Reading order

1. [Glossary](glossary.md)
2. [Architecture levels](architecture/README.md)
3. [L0 system design](architecture/L0-system-design.md)
4. [L1 design plan](architecture/L1-design-plan.md)
5. [Roadmap](roadmap.md)
6. [Architecture Decision Records](adr/README.md)

## Documentation structure

```text
docs/
├── architecture/
│   ├── L0-system-design.md       # 系统目标、边界、参与者与能力
│   ├── L1-design-plan.md         # 容器拆分和下一阶段设计顺序
│   └── modules/                  # L2 模块设计（待逐步补充）
├── adr/                          # 跨模块且难逆转的架构决策
├── templates/                    # 统一的设计文档模板
├── glossary.md                   # 领域术语
└── roadmap.md                    # 设计和交付阶段
```

## Status vocabulary

- `Proposed`：正在讨论，不能作为实现依据。
- `Accepted`：已形成当前基线，变更应通过评审或 ADR。
- `Superseded`：已被新的设计或 ADR 替代。
- `Deferred`：已识别但当前阶段不解决。

除非文档明确标记为 `Accepted`，否则均应视为探索性设计。
