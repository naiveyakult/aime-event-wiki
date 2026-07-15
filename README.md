# AIME Event Wiki

这是一个证据优先、时间安全的多 Agent 系统，用于将清洗后的金融内容整理为带版本的事件 Wiki。项目与私有源数据严格隔离：仓库只包含代码和完全合成的测试夹具。

## 安全边界

禁止提交源 JSONL/Parquet 文件、生成的 Wiki 页面、模型输出、凭证，或从私有语料复制的原文片段。公开测试夹具必须完全合成。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker compose up -d postgres
.venv/bin/alembic upgrade head
.venv/bin/event-wiki --help
```

默认数据根目录为 `../recent_one_year_data`，入库过程只读取源数据。

```bash
event-wiki ingest --month 2025-11
event-wiki candidates
event-wiki run --limit 20
event-wiki link --all --limit 20
event-wiki serve
```

召回规则升级后，可以只删除并重建尚未处理的候选，同时保留已经完成、待审或失败候选的状态：

```bash
event-wiki candidates --rebuild-pending
```

候选召回会优先使用 symbol 与实体身份，并要求新文档同时匹配候选锚点，避免通用财报或公告标题通过传递相似度串联成跨公司的大候选组。

在 `http://127.0.0.1:8000/reviews` 审核 Patch。已批准的版本可通过以下命令导出：

```bash
event-wiki export markdown
event-wiki export structured --cutoff 2025-12-01T00:00:00Z
event-wiki export audit-report
event-wiki export graph
```

审核服务默认只允许本机访问。绑定到非回环地址前，必须设置 `REVIEW_TOKEN` 或传入 `--review-token`。

## 图结构

系统有意区分两种图。LangGraph 是可恢复的执行图，负责事件发现、身份解析、知识抽取、审计、人工审核和提交。PostgreSQL 保存时态知识图谱：带版本的 Event、Entity、Claim、Relation、Evidence 节点通过有证据支撑的边连接。使用 `known_at` 截止时间可以生成历史图快照，避免泄漏后续信息。

## 验证

```bash
ruff check .
ruff format --check .
pytest -q
event-wiki security-scan
```

测试套件包含完全合成的 200 篇文档、30 个事件金标准集，并覆盖图恢复与重跑、乐观锁、时间泄漏、审核冲突、导出和规模回归测试。

## MVP 边界

在引入明确的变更契约之前，`merge_event` 和 `supersede_claim` 会保持故障关闭，Agent 不会静默执行近似操作。真实一个月试运行属于运行发布阶段，需要本地源数据入库、模型凭证和人工审核。生成的数据和指标必须保存在 `.local/` 下，不能进入 Git。

## 当前状态

仓库已实现本地 MVP。事件 Patch 和推断型 Event Link Patch 必须经过人工批准；严格共享证据事实边可以按确定性策略自动提交。项目暂不附带开源许可证；在未来添加许可证之前，保留全部权利。
