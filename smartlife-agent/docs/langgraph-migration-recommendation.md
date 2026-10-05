+# LangGraph 全面化建议与迁移边界
+
+## 结论
+
+建议做**真正但增量式的 LangGraph 化**，不建议一次性重写全部业务代码。
+
+LangGraph 应负责：
+
+- 任务路由、状态流转、循环、条件分支和并行扇出；
+- 可恢复的 human-in-the-loop；
+- 节点级重试、超时、预算和失败隔离；
+- checkpoint、跨进程恢复和跨线程长期状态；
+- 标准化的流式事件、运行追踪和可观测性。
+
+LangGraph 不必接管：
+
+- SQL 生成和商品数据库访问；
+- RAG 检索、融合、重排；
+- Prompt 和模型调用细节；
+- Streamlit 组件布局；
+- MD/向量记忆的具体存储实现。
+
+这些能力保留为普通服务，由图节点调用即可。迁移目标是让它们进入明确的图状态和生命周期，而不是重写成“纯 LangGraph”。
+
+## 当前状态依据
+
+当前环境使用 LangGraph `1.2.12`。
+
+| 位置 | 当前能力 | 主要缺口 |
+| --- | --- | --- |
+| `app/agents/langgraph_orchestrator.py` | `prepare -> execute -> finalize` 顶层图 | `execute` 仍调用旧 `OrchestratorV2._do_process`，路由和业务步骤没有成为节点；图未启用 checkpointer |
+| `app/agents/travel_graph.py` | 旅游规划、执行、反思、修订循环 | 通过回调模拟事件；未作为顶层子图原生组合 |
+| `app/agents/react_graph.py` | agent/tool 循环、工具超时和步数上限 | 仍使用手写回调事件，缺少原生消息流和节点级重试策略 |
+| `app/negotiation/graph.py` | 偏好、共同点、冲突、方案的条件图 | 主要是普通顺序节点，状态更新和并行能力尚未利用 |
+| `app/memory/approval_graph.py` | `interrupt()`、`Command(resume=...)` human-in-the-loop | 只使用 `InMemorySaver`，进程重启或横向扩容后无法恢复 |
+| `app/main.py` | Streamlit 流式展示和记忆审批 UI | 记忆提取在图外执行；UI 手工解析自定义 SSE |
+
+另外，当前图状态中存在 `queue: Any` 等运行期对象，不适合进入持久化状态。启用 checkpoint 前应把运行期队列移出 State，或改用图的流式 writer/宿主适配器。
+
+## 推荐目标图
+
+```mermaid
+flowchart TD
+    START --> load_context
+    load_context --> classify
+    classify --> route
+    route -->|shopping| shopping_subgraph
+    route -->|travel| travel_subgraph
+    route -->|react/general| react_subgraph
+    route -->|negotiation| negotiation_subgraph
+
+    subgraph shopping_subgraph[Shopping]
+        plan_retrieval --> retrieve
+        retrieve -->|SQL + RAG 可并行| fuse_results
+        fuse_results --> generate_answer
+        generate_answer --> validate_answer
+    end
+
+    shopping_subgraph --> finalize_response
+    travel_subgraph --> finalize_response
+    react_subgraph --> finalize_response
+    negotiation_subgraph --> finalize_response
+
+    finalize_response --> extract_memory
+    extract_memory -->|无有效内容| END
+    extract_memory -->|有候选或失败诊断| memory_review
+    memory_review -->|interrupt + resume| persist_memory
+    memory_review -->|reject| END
+    persist_memory --> END
+```
+
+这里的 `extract_memory` 是否执行可以按场景配置。失败节点应进入可展示的错误状态，而不是悄悄返回空列表。
+
+## 关键设计建议
+
+### 1. 顶层 State 只保存可持久化业务状态
+
+建议区分：
+
+- 输入：`user_message`、`user_id`、`session_id`、`scene`；
+- 上下文：`thread_id`、分类结果、检索结果、回答、记忆提取结果；
+- 控制状态：`status`、`error`、重试次数、预算；
+- 流式事件：不直接存 `EventQueue`，由宿主 adapter 或 stream writer 输出。
+
+列表字段如果需要累计，应使用 reducer；如果表示“当前版本”，则保持整体替换。不要混用两种语义。
+
+### 2. 把路由从 Python if/elif 提升为条件边
+
+分类节点输出标准化 `ClassificationResult`，条件路由根据 `intent`、`route` 和置信度进入购物、旅游、ReAct 或协商子图。
+
+低置信度分类可以进入澄清节点；分类失败可以进入保守的通用回答或错误节点。这样路由失败、重试和澄清都可恢复、可追踪。
+
+### 3. 购物链路成为独立子图
+
+建议节点：
+
+1. `plan_retrieval`：根据意图和数据需求决定 SQL/RAG；
+2. `retrieve_sql`、`retrieve_rag`：互不依赖时使用并行分支；
+3. `fuse_results`：统一诊断、商品和评价上下文；
+4. `generate_answer`：流式生成；
+5. `validate_answer`：检查是否引用不存在的商品、是否错误承诺评价信息；
+6. 失败时最多修订一次，超过预算后返回带诊断的安全回答。
+
+SQL、RAG、reranker 保持现有服务接口，不需要重写。
+
+### 4. 记忆提取成为响应后的条件节点
+
+记忆提取应返回结构化状态，例如：
+
+- `ok`：存在候选；
+- `no_content`：正常但无有价值内容；
+- `error`：模型、解析或存储失败。
+
+条件边只在 `ok` 或 `error` 时进入 UI 可见的 Step 4。`no_content` 直接结束，不产生空结果和无价值诊断。
+
+审批继续使用 `interrupt()`，但不要把“开始审批”和“恢复审批”拆成两个互不关联的进程内对象。审批 ID、thread ID 和候选内容要能从持久化 checkpoint 恢复。
+
+### 5. 启用真正的 checkpoint 和 Store
+
+- 开发环境可继续 `InMemorySaver`；
+- 单机生产建议 SQLite checkpointer；
+- 多进程、多副本或需要重启恢复时使用 Postgres checkpointer；
+- thread ID 应稳定绑定 `user_id + scene + browser_session_id`；
+- 需要长期、跨会话共享的信息可评估 LangGraph Store，但不必立即替换现有 MD/向量记忆仓储。
+
+当前只安装了基础 `langgraph-checkpoint`，SQLite/Postgres checkpointer 是独立包。
+
+### 6. 用 LangGraph 流式模式替代手写状态事件协议
+
+建议组合使用：
+
+- `messages`：模型 token 流；
+- `updates`：节点状态更新和 Step 生命周期；
+- `custom`：分类、工具、检索、诊断等领域事件。
+
+Streamlit 层只负责把标准事件映射成 UI。这样“正在生成购物建议”这类 running 状态由节点 started/completed 生命周期表达，不再依赖前端猜测何时清除。
+
+### 7. 加入运行预算和失败策略
+
+- 每个模型节点设置超时和有限重试；
+- 工具继续保留超时，增加可重试/不可重试错误分类；
+- 修订循环设置最大次数；
+- 配置 `recursion_limit`，防止条件边或工具循环失控；
+- 酒店预订等未来敏感操作使用 human approval 和幂等键；
+- 网络工具失败时明确区分真实数据与 `simulated` 降级。
+
+LangGraph 的节点 RetryPolicy 适合瞬时故障，不应重试参数错误、内容违规或用户拒绝。
+
+### 8. 可观测性和评估
+
+- 给节点、模型调用和工具调用添加统一 metadata/tags；
+- 记录 `thread_id`、节点、耗时、token、工具结果摘要和失败原因；
+- 对分类、SQL/RAG、最终回答和记忆提取分别保留评估样本；
+- 如引入 LangSmith 或 OpenTelemetry，应在服务层配置，不让业务节点依赖具体追踪后端。
+
+## 推荐迁移顺序
+
+### 阶段 A：状态和事件契约
+
+- 移除 State 中的运行期 `queue`；
+- 统一节点返回值和错误状态；
+- 为流式事件定义稳定 schema；
+- 为现有行为建立回归样本。
+
+验收：同一请求在旧链路和新图链路的分类、检索数据、最终回答和 UI Step 顺序一致。
+
+### 阶段 B：顶层路由 + 购物子图
+
+- 将 `_do_process` 中的分类、路由和购物流程拆成节点；
+- SQL/RAG 可按需要并行；
+- 加入回答校验和一次受控修订；
+- 旅游、ReAct、协商作为子图接入。
+
+验收：顶层图不再调用旧 `_do_process`，每条路由都有节点级测试和失败路径测试。
+
+### 阶段 C：记忆生命周期
+
+- 记忆提取进入响应后节点；
+- 用条件边控制空结果；
+- 审批 checkpoint 改为持久化；
+- 用户拒绝、部分保存、全部保存和进程恢复均有测试。
+
+### 阶段 D：生产可靠性
+
+- 接入 SQLite/Postgres checkpointer；
+- 加入 RetryPolicy、超时、递归限制和幂等；
+- 接入标准流式模式和追踪；
+- 灰度切换后删除旧编排入口，仅保留兼容适配器。
+
+## 不建议的做法
+
+- 不要为了“全部 LangGraph”重写 SQL/RAG/Prompt/Streamlit；
+- 不要把 EventQueue、数据库连接等不可序列化对象放进持久化 State；
+- 不要把每个简单函数都包装成节点，导致图碎片化；
+- 不要在没有 durable checkpointer 时宣称支持中断恢复；
+- 不要只靠线程内 `InMemorySaver` 支撑多 worker 生产部署；
+- 不要同时保留两套相互独立的记忆提取和审批状态机。
+
+## 最终建议
+
+该项目**值得做真正的 LangGraph 化，但应按状态、路由、子图、持久化、可观测性逐步迁移**。收益最大的部分是：
+
+1. 顶层条件路由与购物子图；
+2. 记忆提取/审批进入同一可恢复生命周期；
+3. durable checkpoint；
+4. 标准流式、重试和运行预算。
+
+完成这些后，LangGraph 才会从“外层包装器”变成项目真正的控制平面。

## 决策细节：为什么不全量改

“全量改 LangGraph”容易被理解成把每个 Python 函数都改成节点。这样会把当前已经稳定的检索服务、数据库访问、模型 Prompt、Streamlit 渲染和记忆文件格式全部卷入迁移，收益却主要来自控制流，不来自重写这些实现。

因此决策边界是：

### 必须改成 LangGraph 控制流

- 分类结果到场景的条件路由；
- 购物的检索规划、SQL/RAG 执行、融合、生成、校验、修订；
- 旅游的规划、执行、反思、修订；
- ReAct 的 agent/tool 循环；
- 记忆提取后的 `ok/no_content/error` 分支；
- 所有需要暂停等待用户决定的操作；
- 节点重试、超时、递归限制和运行预算；
- 可恢复的 checkpoint 和跨进程状态。

### 保留为节点内部服务

- NL2SQL、商品数据库和 SQL 安全校验；
- RAG 文档加载、检索、融合、rerank；
- 模型 Provider、Prompt 和解析；
- Weather、Map、Hotel 等工具实现；
- MD 文件和向量库；
- Streamlit 的视觉布局和组件。

### 不应改成图节点

- 单纯的字符串格式化；
- 一次性的 UI 展示函数；
- 只被一个节点调用且没有分支、恢复、重试需求的内部小函数；
- 配置读取、日志格式化、数据库连接管理。

判断标准不是“能不能放进 StateGraph”，而是这个逻辑是否需要**状态、分支、循环、恢复、重试或审计**。只有满足其中至少一项，才值得成为图节点。

## 审批与敏感操作清单

当前项目中已经有 `MemoryApprovalGraph`，它使用 `interrupt()` 等待用户选择，再通过 `Command(resume=...)` 恢复并写入。这是正确的模式，但目前只覆盖候选记忆审批。

### 已经应该使用 interrupt / Command

| 操作 | 当前实现 | 建议 |
| --- | --- | --- |
| 候选记忆写入 | `MemoryApprovalGraph` 已使用 | 保留；改为 durable checkpoint 后支持重启恢复 |
| 记忆偏好保存 | `save_preference`、`MemoryRepository.save_preference`、`MDMemory.save_preference` | 自动写入前审批；用户明确点击保存时可复用同一审批动作 |
| 记忆事件保存 | `save_event`、`MemoryRepository.save_event`、`LongTermMemory.save_event` | 自动写入前审批，尤其是 purchase/travel 事件 |
| 压缩摘要保存 | `save_compressed_summary`、`MemoryRepository.save_summary` | 生成摘要本身可自动；落盘前需要用户确认 |
| 记忆删除 | `delete_memory` | 删除单条属于破坏性写操作，至少二次确认；建议统一走 destructive approval |
| 清空全部记忆 | `app/main.py` 的清空按钮当前直接删除目录 | 必须改为 interrupt/确认后执行，并记录审计 |


| 酒店预订/占房 | 当前只有 `search_hotels`，没有真正的 booking 工具 | 一旦新增 booking，必须审批；搜索本身不需要审批 |

### 可以自动执行、不需要审批

| 操作 | 原因 |
| --- | --- |
| `get_weather` | 只读、低风险 |
| `get_route` | 只读、低风险 |
| `get_current_time` | 只读、低风险 |
| `search_hotels` | 只读搜索，不产生订单或扣款 |

| `get_order_status` | 只读查询 |
| `get_user_profile`、`get_cross_scene_memories`、`get_user_context` | 只读记忆访问 |
| 商品搜索、评价查询 | 只读 |
| 分类、检索、生成回答 | 无外部副作用 |

### 需要特别注意的现状

- `get_safe_tools()` 只暴露天气、地图、酒店搜索和时间等只读工具。
- `get_all_tools()` 不包含任何支付或创建订单工具。
- Shopping MCP 不再提供 `place_order`，只保留商品搜索、评价查询和只读订单状态。
- `MemoryMCPServer` 中的 `save_preference`、`save_event` 与主应用的 `MemoryRepository` 不是同一条写入路径；应统一到同一审批与仓储层，避免绕过记忆审批。
- 记忆删除和清空比新增记忆风险更高，不能只依赖一个普通按钮。

## 审批状态机建议

不要为每种敏感操作复制一套图。建议统一为：

```text
prepare_action
    ↓
request_approval(interrupt)
    ├─ approve → validate_payload → execute → persist_receipt → END
    ├─ reject → record_rejection → END
    └─ cancel → END
```

审批 payload 至少包含：

- `approval_id`
- `action_type`：memory_write、memory_delete、hotel_booking（后两者仅在产品未来明确需要时启用）
- `user_id`
- 影响对象和数量
- 金额/日期/商品等关键字段
- 是否模拟/降级
- 幂等键
- 可撤销性

批准后的执行必须幂等；同一个 `approval_id` 重复 resume 不得重复写入多条记忆。

## 对“完全 LangGraph”的最终决策

采用“**控制流全 LangGraph，领域实现服务化**”：

1. 顶层路由、子图、记忆生命周期、审批和敏感操作全部由 LangGraph 编排；
2. 检索、模型、工具、存储继续作为普通服务；
3. 逐步替换旧 `_do_process`，但不在第一步删除兼容层；
4. 只有当新图在行为、错误、恢复和性能上通过回归后，才移除旧入口。

这样既能获得 LangGraph 的状态恢复、human-in-the-loop、并行、重试和可观测性，又不会把稳定业务代码无谓重写。

## 开发进度（2026-10-05）

已完成第一批 LangGraph 化：

- 顶层 `LangGraphOrchestrator` 不再用单一 `execute` 节点包裹 `_do_process`。
- 顶层图现在包含 `prepare -> classify -> conditional route -> shopping/travel/negotiation/react/general -> finalize`。
- EventQueue 不再放入持久化 State，而通过 `RunnableConfig` 传递给节点。
- 新增购物子图 `app/agents/shopping_graph.py`：
  - `plan_retrieval`
  - `retrieve`
  - `generate`
  - `validate`
- 购物检索、生成、校验保持现有领域服务实现，没有重写 SQL/RAG/Prompt。
- 旧 `OrchestratorV2._do_process` 仍保留为兼容入口，但已复用新的细粒度 turn 方法。

验证：103 个 pytest 测试通过，Streamlit AppTest 页面加载无异常。

下一批开发目标：

1. 将记忆提取接入响应后的图节点，并用条件边处理 `ok/no_content/error`；
2. 将记忆、删除、清空纳入统一审批状态机；
3. 为顶层图和审批图接入 durable checkpointer；
4. 用 LangGraph `messages/updates/custom` 流式模式替换手写事件协议；
5. 在行为回归稳定后移除 `_do_process` 兼容入口。

### 第二批基础设施

- 新增 `app/approval_graph.py`：通用敏感操作 `interrupt/resume` 状态机。
- `MemoryApprovalGraph` 已改为复用通用审批图，同时保持原有 `start/resume` API。
- 新增 `app/checkpointing.py`：统一创建 InMemory、SQLite、Postgres checkpointer。
- 顶层 `LangGraphOrchestrator` 和记忆审批图现在都通过 checkpoint 工厂获得 checkpointer。
- 已支持同步和异步 executor，便于接入删除记忆等副作用。

当前 checkpoint 默认仍是开发用 InMemorySaver；生产切换 SQLite/Postgres 需要安装对应独立 checkpoint 包，并传入连接配置。

### 第三批记忆生命周期

- 顶层图新增 `extract_memory` 节点，在回答 `finalize` 后执行一次记忆提取。
- 记忆提取结果通过内部 `memory_extraction` 事件回传 UI，UI 不再重复调用小模型。
- `MemoryExtractionResult.as_dict()` 将 `ok/no_content/error`、候选、诊断和展示标记转换为纯数据状态。
- `no_content` 不生成 UI 结果；`candidates` 生成 Step 4 和审批；`error` 生成 Step 4 和失败诊断。
- Streamlit 持久化过程事件会过滤内部 `memory_extraction` 事件。

验证：110 个 pytest 测试通过；UI AppTest 烟测确认图事件被消费、记忆提取只执行一次、空的瞬态状态会被清除。

### 第四批敏感操作审批接入

- 新增 `app/sensitive_actions.py`，统一管理 memory_delete、memory_update、memory_clear、memory_save_summary。
- 所有敏感动作先进入 `ActionApprovalGraph`，批准后才执行。
- 为敏感动作增加 `idempotency_key`，重复批准不会重复执行副作用。
- `MemoryRepository.clear_user_memories()` 同步清理 MD 记忆和向量索引。
- Streamlit 的清空、删除、更新、保存摘要按钮现在只创建审批请求。
- 页面新增统一敏感操作审批面板，批准/拒绝后恢复同一审批 thread。

验证：115 个 pytest 测试通过；Streamlit 交互烟测确认点击清空后先显示审批面板，批准后才执行并清理 pending 状态。
