# 归档（历史文档，不代表现状）

**什么时候读**：只有当你想查"某个功能当初为什么这么做"，或者需要还原旧版本描述时才读。
**不要**把这里的任何内容当作当前设计依据——现行口径在 `README.md`、`smartlife-agent/docs/architecture.md`、`不推送/毕业设计/课题定义.md`。

| 文件 | 原位置 | 归档原因 |
|---|---|---|
| `project-design-v5.md` | 工作区根 | 2026-10 的设计稿，与现行实现多处冲突（图结构、校验范围、修复动作都已变） |
| `trip-gear.md` | `smartlife-agent/docs/` | 2026-10-25 写的行程子系统说明，已并入 `architecture.md` |
| `graph-architecture.md` | `smartlife-agent/docs/` | 分层与五个原生约束已并入 `architecture.md` |
| `QUICKSTART.md` | `smartlife-agent/` | 环境与启动内容已并入 `README.md` 的「快速开始」与「常见问题」 |
| `evaluation-README.md` | `smartlife-agent/data/evaluation/` | 评测数据格式已并入 `architecture.md` 的「评测」一节 |
| `generated/pipeline-spec.{html,json}` | `smartlife-agent/docs/` | 由 `scripts/build_pipeline_spec.py` 生成，随时可重建；留一份快照备查 |

需要更早的版本时用 git：`git log --oneline -- <原路径>`，文件历史都还在。
