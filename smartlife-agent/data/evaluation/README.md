# 评测与实验数据说明

## Rerank 输入格式

每行一个 JSON：

```json
{
  "query": "防水跑步鞋",
  "documents": [{"id": "product-9", "content": "……", "score": 0.1}],
  "relevant_ids": ["product-9"]
}
```

`score` 是原始召回分。项目中的向量基线按距离从近到远排序，因此分数越小越靠前。

## 在线 A/B 记录

设置以下环境变量启用检索曝光日志：

```bash
export SMARTLIFE_AB_LOGGING=true
export SMARTLIFE_AB_SALT="replace-with-a-random-secret"
```

系统会为 `user_id + query + experiment` 做稳定分流：

- `control`：不调用 Cross-Encoder；
- `treatment`：调用 Cross-Encoder；
- 记录 `impression/click/conversion/rejection`；
- 用户 ID 和 session ID 以盐化哈希存储。

点击和转化需要由业务 UI/API 调用 `ExperimentLogger.log_outcome()`。导出可回放数据：

```bash
venv312/bin/python scripts/export_ab_ranking_cases.py \
  --db data/evaluation/ab_events.db \
  --experiment rerank-v1 \
  --output data/evaluation/rerank.from-online.jsonl
```


## 当前产物

- `rerank.business.jsonl`：由当前项目商品数据库和标注查询构建的 Rerank 业务评测集。
- `rerank.business.ab.json`：使用本地缓存 `BAAI/bge-reranker-base` 生成的 Control/Cross-Encoder 对照结果。

## E2E 性能报告

`scripts/run_e2e_benchmark.py` 生成的 JSON 使用两类延迟口径：

- `latency_ms`：完整 benchmark sample 的总体 P50/P95/P99；
- `metric_latency_ms`：关键体验指标和图节点耗时的分项 P50/P95/P99。

关键体验指标包括：

- `timing.time_to_first_token_ms`：用户看到第一个正文 Token；
- `timing.response_ready_ms`：用户拿到完整回答；
- `timing.post_response_ms`：回答完成后的记忆提取等后处理；
- `timing.pipeline_complete_ms`：后处理也结束的完整链路时间。

节点指标使用 `node.<graph>:<node>` 命名，例如
`node.shopping:retrieve`、`node.shopping:generate` 和
`node.langgraph:classify`。每条 sample 的原始值保留在
`samples[].metadata.node_durations_ms`，便于按 workload 回溯。
