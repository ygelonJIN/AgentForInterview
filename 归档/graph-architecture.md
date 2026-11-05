# 纯 LangGraph 架构

## 一句话

控制流、状态、并发、重试、超时、流式、人机中断全部交给 LangGraph；
业务能力（分类、检索、生成、记忆读写）下沉为无编排的服务层。
UI 只驱动一张编译后的图，不再持有任何编排对象。

## 分层

```text
app/main.py（Streamlit）
   │  只调用 TurnGraph：process_streaming / start_memory_approval / resume_memory_approval
   ▼
app/graphs/orchestrator.py  TurnGraph
   │  build_turn_graph()：StateGraph(TurnState, context_schema=TurnContext)
   ▼
app/graphs/subgraphs/*      子图（购物 / 旅行 / 装备 / 复合 / 客服 / 通用规划 / ReAct / 通用对话）
   │  节点签名统一 (state, runtime: Runtime[TurnContext])
   ▼
app/services/turn_services.py  TurnServices（无编排的领域能力）
   │
   ├─ app/retrieval/*   RAG / NL2SQL / 检索服务
   ├─ app/memory/*      短期、长期、MD、压缩、提取、审批图
   └─ app/tools/*       工具注册表（按 Actor 绑定）
```

## 五个"原生"约束

| 维度 | 原生做法 | 已废弃做法 |
| --- | --- | --- |
| 依赖注入 | `StateGraph(..., context_schema=TurnContext)` + `runtime.context` | 编排器实例属性 + `config["configurable"]` |
| 流式 | `runtime.stream_writer(...)` + `astream(stream_mode="custom", subgraphs=True)` | 自研 `EventQueue` + `config` 注入 |
| 重试/超时 | `add_node(..., retry_policy=RetryPolicy(...), timeout=...)` | 自研 `run_policy_node` 包装器 |
| 节点计时 | `stream_mode="tasks"` 的 `node_started` / `node_succeeded` | 包装器里手写 `node_succeeded` 日志 |
| 并发复合 | 并行边 + `branch_results` reducer | 一个节点里 `asyncio.gather` 两个 legacy 方法 |
| 人机中断 | `interrupt()` + `Command(resume=...)` + checkpointer | 直接调用审批对象方法 |

## 顶层图

```text
START → prepare → classify ─┬─ shopping          ─┐
                            ├─ travel            ─┤
                            ├─ trip_gear         ─┤
                            ├─ composite（并行）  ─┼→ finalize ─┬→ END
                            ├─ customer_service  ─┤             └→ extract_memory → END
                            ├─ general_plan      ─┤
                            ├─ react             ─┤
                            └─ general_chat      ─┘
```

- `prepare`：建 thread、记录用户消息、一次性派生共享约束（购物/旅行共用同一份）。
- `classify`：统一分类 + 场景约束 + 关键词安全网（逻辑在服务层，节点只搬运）。
- `composite`：两条并行边分别跑购物/旅行子图，`branch_results` 用 reducer 合并，单分支失败不拖垮另一分支。
- `finalize`：保存助手回复、发 `done`；`extract_memory` 由入口按需触发，不占关键路径。

## 事件契约

custom chunk 的结构与旧 SSE 完全一致，UI 无需改动：

```json
{"event": "token", "data": {"token": "你"}, "step": "generate", "timestamp": 0.0, "trace_id": "..."}
```

子图的 custom 事件靠 `subgraphs=True` 原生透传，命名空间在 `_split_chunk` 里被剥掉。

## 新增一个场景子图的步骤

1. 定义状态：通用字段放 `app/graphs/state.py`，只有该场景用到的字段就近写在子图模块里（如 `TravelPlanState`）。
2. 在 `app/graphs/subgraphs/` 写节点，签名 `(state, runtime: Runtime[TurnContext])`，事件用 `runtime.context.sink_for(runtime)`。
3. `build_xxx_graph()` 里用 `retry_policy` / `timeout` 声明策略。
4. 在 `app/graphs/orchestrator.py` 的 `ROUTE_TARGETS` 里挂上节点与路由名。
5. 在 `app/graphs/nodes.py` 的 `route()` 里给出条件。

## 迁移状态

已完成，`app/agents/` 目录已移除：

| 原模块 | 现在 |
| --- | --- |
| `agents/langgraph_orchestrator.py` | `graphs/orchestrator.py`（`TurnGraph`） |
| `agents/orchestrator_v2.py` | `services/turn_services.py`（`TurnServices`，无编排） |
| `agents/shopping_graph.py` | `graphs/subgraphs/shopping.py` |
| `agents/react_graph.py` | `graphs/subgraphs/react.py` |
| `agents/general_plan_graph.py` | `graphs/subgraphs/general_plan.py` + `graphs/plan_models.py` |
| `agents/customer_service_graph.py` | `graphs/subgraphs/customer_service.py` |
| `agents/travel_graph.py` | `graphs/subgraphs/travel.py`（plan/reflect/revise 全在图里） |
| `agents/travel_agent.py` | `services/travel_service.py`（只留能力） |
| `agents/task_models.py` | `services/task_models.py` |
| `trip_gear/graph.py` | `graphs/subgraphs/trip_gear.py` |
| `trip_gear/agent.py` | 保留为 `TripGearAgent` 服务（只留能力） |
| `execution_log.run_policy_node` / `CallbackEventQueue` | 删除，改用 LangGraph 原生 `retry_policy` / `timeout` + `tasks` 流 |

依赖注入约定：生产环境从 `services` 取能力；测试用 `TurnContext.extra` 覆盖
`planner` / `reflector` / `executor` / `context_builder` / `product_search` / `max_revisions` 等。
