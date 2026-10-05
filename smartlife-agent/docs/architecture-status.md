# 架构状态与代码边界

## Current

- UI：`app/main.py`
- 顶层控制流：`app/agents/langgraph_orchestrator.py`（分类 + 条件路由 + 场景节点 + finalize）
- 购物子图：`app/agents/shopping_graph.py`（检索规划 -> 检索 -> 生成 -> 校验）
- 当前编排实现：`app/agents/orchestrator_v2.py`（领域服务和兼容入口）
- 检索服务：`app/retrieval/service.py`
- 旅游子图：`app/agents/travel_graph.py`
- ReAct 子图：`app/agents/react_graph.py`
- 通用敏感操作审批图：`app/approval_graph.py`
- 记忆审批适配器：`app/memory/approval_graph.py`
- Checkpoint 工厂：`app/checkpointing.py`
- 敏感操作审批服务：`app/sensitive_actions.py`
- 记忆提取生命周期：顶层图 `extract_memory` 节点
- 记忆一致性仓储：`app/memory/repository.py`

## Compatibility

- `app/agents/orchestrator.py`：旧同步编排器，仅保留兼容调用。
- `app/agents/shopping_agent.py`：旧同步购物链路。
- `app/agents/reflection.py`：旧反思实现。
- `app/retrieval/fusion.py`：旧 `HybridRetriever` facade，内部委托 `RetrievalService`。

## Target / Not Yet Real

- MCP 动态服务发现尚未接入当前 UI 主链路。
- 天气、地图支持通过 `SMARTLIFE_WEATHER_API_URL`、`SMARTLIFE_ROUTE_API_URL` 接入 HTTP provider。
- 未配置或 provider 失败时使用本地模拟，结果带 `simulated: true`。
- 项目不提供支付或创建订单能力；酒店仅支持 `SMARTLIFE_HOTEL_API_URL` provider 的只读搜索，未配置时明确模拟降级。

## Owner Rules

- 新业务逻辑只进入 Current 组件。
- Compatibility 文件只接受兼容性修复，不新增能力。
- 目标能力只有在存在真实调用方、测试和运行时证据后才能移入 Current。
