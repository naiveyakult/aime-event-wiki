# 系统架构

## 两种图，各司其职

LangGraph 是持久化工作流图，负责 Agent 执行、checkpoint、人工中断、重试和恢复。Event Wiki 知识图谱是领域模型，连接带版本的事件、实体、原子 Claim、Relation 和不可变 Evidence。

```text
证据 ──支持──> 主张 ──描述──> 事件
 │                            │
 └────────支持────────────────┤
                              ├──影响──> 实体
实体 ──供应商/客户/合作伙伴等──> 实体
事件 ──贡献/对冲/更新/共同驱动等──> 事件
```

知识图谱节点和边保存在 PostgreSQL 中，因此审核批准、乐观版本控制和历史截止时间查询都可以在事务中完成。`event_edges` 是属性边投影，保存 `known_at`、`valid_from` 和 Evidence ID。Graph JSON 和 Markdown 都是派生导出。未来可以把 Neo4j 作为只读投影加入，但它不能成为唯一事实源。

## 时间不变量

每个导出的事实或边都必须满足 `known_at <= prediction_cutoff`。`event_time` 表示事情发生的时间，`known_at` 表示市场最早可能知道该信息的时间，两者不能互换。

## 写入权限

Agent 只能提出通过 Schema 验证的 `WikiPatch`。确定性提交器负责校验 Schema、Evidence 外键、时间约束、审计结果和 `base_version`。事件 Patch 必须在人工审核节点暂停，批准后才能提交新的 Wiki 版本。跨事件链接使用独立版本：只有同一篇已批准 Evidence 明确支持两个端点、逐字引用可定位、置信度不低于 0.9 的事实边可以由确定性策略自动提交；推断边仍进入人工审核。

## 跨事件关联

Event Link Agent 只在已经提交的事件之间工作。候选通过时间、symbol、主体、关系实体和文本关键词确定性召回，再由 Agent 提议 `causes`、`contributes_to`、`counteracts`、`amplifies`、`contradicts`、`same_driver`、`evidence_update` 或 `temporal_sequence`。低风险推断边可以批量审核，高风险因果边始终单条审核，低置信度、无引用和机械传递边不会生成 Patch。
