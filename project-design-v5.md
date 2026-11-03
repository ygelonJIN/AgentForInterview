# SmartLife Agent — 全栈智能体电商旅游平台 v5

> **文档状态（2026-10-05；2026-10-25 增补 trip_gear）**
> - Current：`smartlife-agent/app/main.py -> LangGraphOrchestrator -> OrchestratorV2` 是当前入口。
> - Current：混合语义路由、shopping+travel 复合意图并行、Agent 专属 MCP 工具能力面、跨 Agent 工具隔离和运行时拒绝已实现。
> - Current（2026-10-25）：新增 `trip_gear`（行程约束派生与装备协同）链路，详见 `smartlife-agent/docs/trip-gear.md`；装备类请求走有向链，不再走 shopping+travel 并行分支。
> - Target：远端 MCP Agent 的动态注册/发现与消息路由、外部酒店服务、完整跨场景采购联动仍是目标能力。
> - Deprecated：`app/agents/orchestrator.py` 是旧兼容实现，不属于当前主链路。
> - Removed/Unwired：旧的 MCP 动态发现和未接线组件不得被视为已完成功能。

## 项目定位

> 基于 LangChain Core 与 LangGraph 构建的 Multi-Agent 智能体系统，统一覆盖**电商购物**和**旅游出行**两大场景。
>
> 毕业设计聚焦其中的 `trip_gear` 链路：约束派生 → 逐日行程 → 装备推导 → 已有判定 → 缺口检索 → 校验修复。

---

## 一、为什么合并？——三个不可拆分的理由

| 理由 | 具体场景 | 为什么拆不开 |
|------|---------|-------------|
| **记忆驱动的跨场景推荐** | 用户上周买了帐篷和登山杖，这周问"周末去哪玩" | 记忆存在同一个向量库里，旅游 Agent 能直接召回购物场景的语义，推荐露营路线 |
| **统一预算与采购联动** | 用户说"我要露营，帮我列装备清单+行程" | 购物清单和行程规划需要同步预算；装备类请求走 `trip_gear` 有向链（约束→行程→装备→已有→缺口→校验），普通 shopping+travel 才并行 |

---

## 二、技术栈与映射

| 技术 | 在项目中的真实用途 | 面试深度切入点 |
|------|------------------|--------------|
| **ReAct Agent** | 主推理引擎，处理大多数交互式对话和导购 | ReAct 的 Thought-Action-Observation 循环、与 CoT 的区别 |
| **Plan & Execute Agent** | 多步复杂任务规划（行程规划、预算分配、采购清单） | Plan 的分解策略、Execute 的回退机制、与 ReAct 的选择标准 |
| **结构化审核与硬校验** | 复杂任务结果的自检和优化 | 生成行程/清单后自检合理性，不满意则重新规划 |
| **RAG + Rerank + Top K** | 非结构化内容检索（用户评价、攻略、FAQ） | 粗排 + 精排的设计、Cross-Encoder vs Bi-Encoder 的 trade-off |
| **NL2SQL** | 结构化商品查询（价格、分类、库存） | 自然语言到 SQL 的准确性、与 RAG 的分工 |
| **MCP** | 每个 Agent 暴露独立工具能力面，跨 Agent 强隔离 | MCP Server 构建、tools/list、Agent Tool Policy、执行层授权 |
| **Function Calling** | 天气、地图、时间及只读订单查询 | 工具描述设计、参数约束、错误处理 |
| **JSON Schema** | 工具参数定义、LLM 输出格式约束 | 保证输出结构化，减少解析错误，与 Pydantic 的配合 |
| **长期记忆** | 用户画像 + 跨会话摘要记忆 + 跨场景推荐 | 摘要压缩策略、检索质量保证、记忆淘汰机制 |

---

## 三、系统架构

```text
┌─────────────────────────────────────────────────────────────────────┐
│                         Streamlit Web UI                             │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │ prepare / classify   │
                    └──────────┬──────────┘
                               │
        ┌──────────────────────▼───────────────────────┐
        │ UnifiedClassifier                             │
        │ 关键词 → 上下文 → 小模型 → 主模型仲裁           │
        │ 输出 intents / primary_intent / route / ...    │
        └──────────────────────┬───────────────────────┘
                               │
                 ┌─────────────▼─────────────┐
                 │ LangGraphOrchestrator._route│
                 └───┬──────┬──────┬──────┬───┘
                     │      │      │      │
             ┌───────▼─┐ ┌──▼────┐ ┌▼──────────────┐ ┌─▼────────────┐
             │Shopping │ │Travel │ │Shopping+Travel │ │Customer /    │
             │ Agent   │ │ Agent │ │并行执行并合并    │ │General/React │
             └───────┬─┘ └──┬────┘ └┬──────────────┘ └─┬────────────┘
                     │      │      │                   │
        ┌────────────▼──────▼──────▼───────────────────▼────────────┐
        │              Agent 专属 Tool Registry                       │
        │ shopping / travel / general 工具能力面严格隔离               │
        └────────────┬──────────────────────────┬───────────────────┘
                     │                          │
          ┌──────────▼──────────┐    ┌──────────▼──────────┐
          │ Hybrid Retrieval     │    │ Memory / Approval    │
          │ NL2SQL + RAG + Rank  │    │ Short / Long / MD    │
          └─────────────────────┘    └─────────────────────┘

旁路能力（不在主聊天消息链中）：
外部 MCP Client ─→ shopping/travel/general 独立 MCP Server
                  tools/list 与 call_tool 均受 Agent Tool Policy 约束
```

关键边界：

- `intent` / `primary_intent` 决定 Agent；
- `route` 决定 ReAct 或 Plan & Execute；
- `retrieval` 决定 SQL、RAG、混合或不检索；
- `sub_intent=gear_plan` 决定是否进入 `trip_gear`（约束派生 → 行程 → 装备 → 校验），见 `smartlife-agent/docs/trip-gear.md`；
- `shopping + travel` 是当前唯一并行双 Agent 分支；
- MCP Server 是对外能力面，主聊天当前仍使用进程内 Registry 和静态 `bind_tools()`。

---

## 四、任务路由：混合分类决定 Agent 与执行范式

当前不是纯关键词路由，也不是每次都调用大模型做语义路由，而是四层混合分类：

```text
关键词快筛
  → 上下文/指代沿用
  → 小模型语义分类
  → 低置信或格式异常时主模型仲裁/修复
  → 默认兜底
```

分类器一次输出三组互相独立的结果：

```python
class ClassificationResult(BaseModel):
    intents: list[str]          # 1-2 个明确意图，决定使用哪些 Agent
    primary_intent: str         # 主意图，决定默认执行 Agent
    route: str                  # react / plan_and_execute
    retrieval: str              # sql_only / rag_only / mixed / none
    sub_intent: str             # search / plan_trip / gear_plan / ...
    confidence: float
    reason: str
```

### 三层职责

| 字段 | 职责 | 示例 |
|---|---|---|
| `intents` / `primary_intent` | 决定 Agent | `shopping`、`travel`、`customer_service`、`general` |
| `route` | 决定执行范式 | `react`、`plan_and_execute` |
| `retrieval` | 决定数据获取方式 | `sql_only`、`rag_only`、`mixed`、`none` |

> 装备类请求额外把 `sub_intent` 置为 `gear_plan`，由 `_route()` 优先分流到 `trip_gear`；上表三组字段的职责不变。

### 条件路由

LangGraph 的 `_route()` 不再调用模型，而是消费分类结果：

```text
sub_intent = gear_plan         → trip_gear
intents 包含 shopping + travel → shopping_travel
primary_intent = shopping      → shopping
primary_intent = travel        → travel
primary_intent = customer_service → customer_service
primary_intent = general + react → react
primary_intent = general + plan_and_execute → general_plan
其他                            → general
```

多意图最多保留两个。当前真正并行执行两个 Agent 的分支是 `shopping + travel`：

1. Shopping Agent 查询商品、评价或订单；
2. Travel Agent 查询天气、活动并生成行程；
3. 两个分支并行运行；
4. 最终按“购物推荐 / 旅行计划”两个分区合成回答。

其他复合表达仍以 `primary_intent` 为主，避免任意意图组合导致不可控的 Agent 扇出。

装备类请求（`gear_plan`）优先进入 `trip_gear`，不再走 `shopping + travel` 并行分支。

### 示例

| 用户请求 | 分类结果 | 执行 |
|---|---|---|
| “推荐一双跑步鞋” | `shopping` | Shopping Agent |
| “帮我规划杭州两天行程” | `travel + plan_and_execute` | Travel Agent |
| “推荐跑鞋，再规划杭州两日游” | `shopping + travel` | 两个 Agent 并行 |
| “周末去莫干山露营，帮我列装备清单” | `travel + gear_plan` | `trip_gear` 链路 |
| “订单到哪里了” | `customer_service` | Customer Service Agent |
| “杭州明天天气怎么样” | `general + react` | 通用 ReAct |

---

## 五、MCP 协议：Agent 专属工具能力面

### 当前实现

每个 Agent 可以构建为独立 MCP Server，并且只发布自己的工具：

| Agent | 工具能力面 |
|---|---|
| `shopping` | `search_products`、`get_product_reviews`、`get_order_status` |
| `travel` | `search_destinations`、`get_local_activities`、`plan_itinerary`、`get_weather`、`get_route`、`get_current_time` |
| `general` | `get_weather`、`get_route`、`get_current_time` |

启动方式：

```bash
venv312/bin/python scripts/run_mcp_server.py --transport stdio --agent shopping
venv312/bin/python scripts/run_mcp_server.py --transport stdio --agent travel
```

代码入口：

```python
from app.mcp_servers.protocol_server import build_mcp_server

shopping_server = build_mcp_server(agent_id="shopping", actor_id="user_001")
travel_server = build_mcp_server(agent_id="travel", actor_id="user_001")
```

### 三层工具隔离

1. **MCP 发现层**：`tools/list` 只返回当前 Agent 的工具；
2. **模型绑定层**：`bind_tools()` 只绑定当前 Agent 的工具；
3. **执行授权层**：即使模型被提示注入或伪造 `tool_call`，跨 Agent 工具也会被拒绝，不会真正调用。

```python
policy = get_agent_tool_policy("shopping")
policy.require("get_route")
# ToolAuthorizationError: 工具越权: get_route 不属于 Agent shopping
```

### 即插即用边界

当前“即插即用”是指外部 MCP 客户端可以独立连接、替换或升级某个 Agent MCP Server，且不会看到或调用其他 Agent 的工具。

主聊天链路目前仍由 LangGraph 使用进程内 Agent Registry 和静态 `bind_tools()` 执行，不依赖 MCP 网络调用。远端 MCP Agent 的自动注册、动态发现、能力协商和消息路由是后续目标，不应宣称已经实现。

---

## 六、混合检索：NL2SQL + RAG + Rerank

### 核心设计：结构化数据用数据库，非结构化数据用 RAG

| 数据类型 | 存储方式 | 查询方式 | 例子 |
|---------|---------|---------|------|
| 商品信息 | 关系型数据库 | NL2SQL | "500块以内防水跑步鞋" → SELECT * FROM products WHERE price<500 AND category='跑步鞋' AND waterproof=true |
| 用户订单 | 关系型数据库 | SQL | SELECT * FROM orders WHERE user_id=? AND status='shipped' |
| 用户画像 | 关系型数据库 | SQL | SELECT * FROM users WHERE id=? |
| 用户评价 | 向量数据库 | RAG | "防水性好的" → 检索到"下雨天穿完全没问题" |
| 旅游攻略 | 向量数据库 | RAG | "适合情侣的地方" → 检索到攻略里的推荐 |
| 客服 FAQ | 向量数据库 | RAG | "怎么退货" 约等于 "不想要了能退吗" |
| 活动描述 | 向量数据库 | RAG | "周末亲子活动" → 语义匹配 |

### 混合检索流程

```
用户："帮我找 500 块以内防水的跑步鞋，最好评价说耐磨的"
                │
                ▼
        ┌───────────────┐
        │  意图拆解       │
        │  结构化条件：    │
        │  price < 500   │
        │  category = 跑步鞋 │
        │  waterproof = true │
        │                  │
        │  语义条件：       │
        │  "评价说耐磨的"   │
        └───────┬─────────┘
                │
        ┌───────▼─────────┐
        │  SQL 查询        │──→ 结构化结果（10双鞋）
        │  RAG 检索评价     │──→ 语义结果（耐磨相关评价）
        └───────┬─────────┘
                │
        ┌───────▼─────────┐
        │  结果融合         │
        │  按商品ID关联     │
        │  合并评分         │
        └───────┬─────────┘
                │
        ┌───────▼─────────┐
        │  Rerank 精排     │
        │  Cross-Encoder   │
        │  Top K 输出      │
        └─────────────────┘
```

### NL2SQL 实现

```python
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

class SQLQuery(BaseModel):
    """NL2SQL 输出"""
    sql: str = Field(description="生成的 SQL 查询")
    explanation: str = Field(description="查询解释")
    needs_rag: list[str] = Field(default=[], description="需要 RAG 检索的语义条件")

nl2sql_prompt = ChatPromptTemplate.from_messages([
    ("system", """你是一个 SQL 生成器。根据用户的自然语言描述，生成对应的 SQL 查询。

数据库表结构：
- products(id, name, category, price, waterproof, brand, stock, description)
- reviews(id, product_id, user_id, content, rating, created_at)
- orders(id, user_id, product_id, quantity, status, created_at)
- users(id, name, age, preferences, budget)

规则：
1. 只生成 SELECT 语句
2. 使用参数化查询防止 SQL 注入
3. 结构化条件（价格、分类、品牌）用 WHERE 子句
4. 语义条件（"耐磨"、"舒适"、"好看"）不要放到 SQL 里，放到 needs_rag 字段"""),
    ("user", "{user_query}")
])

nl2sql_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
nl2sql_chain = nl2sql_prompt | nl2sql_llm.with_structured_output(SQLQuery)
```

### RAG 实现（Rerank + Top K）

```python
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import CrossEncoderReranker
from langchain_community.cross_encoders import HuggingFaceCrossEncoder

# 1. 向量检索（粗排，召回较多结果）
vectorstore = Chroma.from_documents(documents, embedding=OpenAIEmbeddings())
retriever = vectorstore.as_retriever(search_kwargs={"k": 20})  # 召回20条

# 2. Cross-Encoder Rerank（精排）
cross_encoder = HuggingFaceCrossEncoder(model_name="BAAI/bge-reranker-base")
reranker = CrossEncoderReranker(model=cross_encoder, top_n=5)  # 精排后取 Top 5

# 3. 组合
compression_retriever = ContextualCompressionRetriever(
    base_compressor=reranker,
    base_retriever=retriever
)

# 4. 使用
docs = compression_retriever.invoke("防水性好的跑步鞋评价")
```

### 面试话术

"商品的基础属性用 NL2SQL 精确筛选，用户评价和攻略用 RAG 语义检索，两边结果按商品ID融合后用 Cross-Encoder Rerank 精排。粗排用 Bi-Encoder 召回 20 条，精排用 Cross-Encoder 取 Top 5，平衡了速度和精度。Bi-Encoder 速度快但精度低，适合粗排；Cross-Encoder 精度高但速度慢，适合精排。"

---

## 七、ReAct Agent 实现

### 导购场景

```python
from langchain.agents import AgentExecutor, create_react_agent
from langchain_core.prompts import ChatPromptTemplate

react_prompt = ChatPromptTemplate.from_messages([
    ("system", """你是一个智能导购助手。使用以下工具帮助用户找到合适的商品。

工具使用规则：
1. 先理解用户需求，提取结构化条件（价格、分类、品牌）和语义条件（风格、用途）
2. 结构化条件用 NL2SQL 查询数据库
3. 语义条件用 RAG 检索评价
4. 两边结果融合后推荐给用户

可用工具：{tools}
工具名称：{tool_names}"""),
    ("user", "{input}"),
    ("assistant", "{agent_scratchpad}")
])

agent = create_react_agent(llm=ChatOpenAI(model="gpt-4o"), tools=mcp_tools, prompt=react_prompt)
agent_executor = AgentExecutor(agent=agent, tools=mcp_tools, verbose=True)
```

### 客服场景

```python
customer_service_prompt = ChatPromptTemplate.from_messages([
    ("system", """你是一个智能客服。可以帮用户：
1. 查询订单状态
2. 解答退换货政策
3. 推荐相关商品
4. 处理投诉和建议

使用 RAG 检索客服 FAQ 和商品信息，使用 NL2SQL 查询订单数据。

可用工具：{tools}
工具名称：{tool_names}"""),
    ("user", "{input}"),
    ("assistant", "{agent_scratchpad}")
])
```

---

## 八、Plan & Execute Agent 实现

### 行程规划场景

```python
from langchain_experimental.plan_and_execute import PlanAndExecute, load_agent_executor, load_chat_planner

planner = load_chat_planner(ChatOpenAI(model="gpt-4o"))
executor = load_agent_executor(ChatOpenAI(model="gpt-4o"), tools=mcp_tools, verbose=True)

agent = PlanAndExecute(planner=planner, executor=executor, verbose=True)

# 用户请求："我和女朋友周末去杭州玩两天，预算3000"
# Plan 输出：
# Step 1: 查询杭州周末天气
# Step 2: 搜索适合情侣的景点
# Step 3: 查询本地活动
# Step 4: 排出两天行程
# Step 5: 计算费用，确保不超预算
# Step 6: 生成最终行程单

result = agent.invoke({"input": "我和女朋友周末去杭州玩两天，预算3000"})
```

### 采购清单场景

```python
# 用户请求："我要装修新房，预算5万，帮我列采购清单"
# Plan 输出：
# Step 1: 确认房间类型和面积
# Step 2: 列出需要的品类（家具、家电、装饰）
# Step 3: 每个品类搜索推荐商品
# Step 4: 计算总价
# Step 5: 如果超预算，调整推荐
# Step 6: 生成最终采购清单
```

---

## 九、结构化审核与结果自检

### 为什么需要 Reflection？

Plan & Execute 生成的行程/清单可能有不合理的地方（行程太赶、预算超支、时间冲突）。旅行子图与通用规划子图通过结构化审核和确定性硬校验完成自检（现实现：`app/graphs/subgraphs/travel.py`、`app/graphs/subgraphs/general_plan.py`）。

> 2026-10-25 增补：`trip_gear` 不依赖模型自审，改用确定性校验（预算含装备、时间重叠、天数、露营必备装备）加两轮有界修复；本节描述的是旅行/通用规划子图的审核方式，详见 `smartlife-agent/docs/trip-gear.md`。

### 实现

```python
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

class ReflectionResult(BaseModel):
    """反思结果"""
    is_satisfactory: bool = Field(description="结果是否满意")
    issues: list[str] = Field(description="发现的问题")
    suggestions: list[str] = Field(description="改进建议")
    revised_plan: str = Field(description="修订后的计划，如果需要的话")

reflection_prompt = ChatPromptTemplate.from_messages([
    ("system", """你是一个计划审核专家。检查以下计划是否合理。

检查要点：
1. 时间安排是否合理（不要一天排8个景点）
2. 预算是否超支
3. 地理位置是否方便（不要在城市两端来回跑）
4. 是否有遗漏（比如没安排吃饭时间）
5. 天气是否适合户外活动

原始请求：{original_request}
生成的计划：{plan}"""),
    ("user", "请审核这个计划，指出问题并给出改进建议。")
])

reflection_llm = ChatOpenAI(model="gpt-4o")
reflection_chain = reflection_prompt | reflection_llm.with_structured_output(ReflectionResult)

def reflect_and_improve(original_request: str, plan: str, max_iterations: int = 3) -> str:
    """反思-改进循环"""
    current_plan = plan
    for i in range(max_iterations):
        result = reflection_chain.invoke({
            "original_request": original_request,
            "plan": current_plan
        })
        if result.is_satisfactory:
            return current_plan
        current_plan = regenerate_plan(original_request, current_plan, result.suggestions)
    return current_plan
```

### 面试话术

"Plan & Execute 生成的计划可能有不合理的地方，比如行程太赶或预算超支。结构化审核会检查这些问题，如果有问题就用改进建议重新生成计划，最多迭代3次。这样保证了最终输出的质量。"

---

## 十、Function Calling：外部工具调用

### 工具定义（JSON Schema）

```python
from langchain_core.tools import tool
from pydantic import BaseModel, Field

class WeatherInput(BaseModel):
    """天气查询参数"""
    city: str = Field(description="城市名称")
    date: str = Field(description="日期，格式 YYYY-MM-DD")

@tool("get_weather", args_schema=WeatherInput)
def get_weather(city: str, date: str) -> dict:
    """获取指定城市和日期的天气信息"""
    import requests
    response = requests.get(f"https://api.weather.com/v1/{city}/{date}")
    return response.json()

class MapInput(BaseModel):
    """地图查询参数"""
    origin: str = Field(description="起点")
    destination: str = Field(description="终点")
    mode: str = Field(description="交通方式：driving/walking/transit")

@tool("get_route", args_schema=MapInput)
def get_route(origin: str, destination: str, mode: str) -> dict:
    """获取两点之间的路线和距离"""
    ...

class TimeInput(BaseModel):
    """时间查询参数"""
    timezone: str = Field(default="Asia/Shanghai", description="时区")

@tool("get_current_time", args_schema=TimeInput)
def get_current_time(timezone: str = "Asia/Shanghai") -> dict:
    """获取当前时间"""
    from datetime import datetime
    import pytz
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    return {"datetime": now.isoformat(), "timezone": timezone}
```

---

## 十一、长期记忆系统

### 记忆类型

| 记忆类型 | 存储方式 | 生命周期 | 用途 |
|---------|---------|---------|------|
| 短期记忆 | 内存（对话历史） | 单次会话 | 当前对话上下文 |
| 长期记忆-摘要 | 向量数据库 | 永久 | 跨会话的用户偏好摘要 |
| 长期记忆-事件 | 向量数据库 | 永久 | 重要事件（购买、旅行、打卡） |

### 实现

```python
from langchain.memory import ConversationSummaryBufferMemory
from langchain_community.vectorstores import Chroma

class LongTermMemory:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.vectorstore = Chroma(
            collection_name=f"user_{user_id}",
            embedding_function=OpenAIEmbeddings()
        )
    
    def save_summary(self, conversation: str):
        """保存对话摘要"""
        summary = llm.summarize(conversation)
        self.vectorstore.add_texts(
            texts=[summary],
            metadatas=[{"type": "summary", "user_id": self.user_id}]
        )
    
    def save_event(self, event: dict):
        """保存重要事件"""
        text = f"用户{event['action']}了{event['object']}，时间{event['time']}"
        self.vectorstore.add_texts(
            texts=[text],
            metadatas=[{"type": "event", **event}]
        )
    
    def recall(self, query: str, k: int = 5) -> list:
        """根据查询召回相关记忆"""
        return self.vectorstore.similarity_search(query, k=k)
    
    def get_cross_scene_memories(self, current_scene: str) -> list:
        """获取跨场景记忆"""
        if current_scene == "travel":
            return self.recall("购物 装备 商品 购买")
        elif current_scene == "shopping":
            return self.recall("旅行 目的地 景点 活动")
```

### 跨场景推荐示例

```
用户上周买了帐篷和登山杖
        ↓
记忆存储：{"action": "购买", "object": "帐篷、登山杖", "scene": "shopping"}
        ↓
用户这周问："周末去哪玩？"
        ↓
旅游 Agent 调用 get_cross_scene_memories("travel")
        ↓
召回记忆：用户买了帐篷和登山杖
        ↓
推荐：适合露营的地点（而不是海边度假）
```

---


## 十三、商品筛选功能

### 手动分类筛选（保留原有功能）

```python
CATEGORIES = {
    "服装": {
        "男装": ["T恤", "衬衫", "裤子", "外套"],
        "女装": ["连衣裙", "上衣", "裙子", "外套"],
        "运动": ["跑步鞋", "运动裤", "瑜伽服"]
    },
    "户外": {
        "露营": ["帐篷", "睡袋", "炉具"],
        "登山": ["登山杖", "登山鞋", "背包"],
        "水上": ["泳衣", "浮潜装备"]
    },
    "电子": {
        "手机": ["iPhone", "小米", "华为"],
        "电脑": ["笔记本", "台式机", "平板"]
    }
}
```

### 智能导购（自然语言筛选）

```python
def intelligent_shopping_guide(user_query: str) -> dict:
    """智能导购：自然语言 → 结构化筛选 + 语义检索"""
    # 1. NL2SQL 提取结构化条件
    sql_result = nl2sql_chain.invoke({"user_query": user_query})
    
    # 2. 执行 SQL 查询
    products = execute_sql(sql_result.sql)
    
    # 3. 如果有语义条件，RAG 检索评价
    if sql_result.needs_rag:
        rag_results = compression_retriever.invoke(" ".join(sql_result.needs_rag))
        products = enrich_with_reviews(products, rag_results)
    
    # 4. Rerank 精排
    products = rerank_products(products, user_query)
    
    return {"products": products[:5]}  # Top K = 5
```

---

> 2026-11-03 增补：`app/agents/` 已整体迁移为 `app/graphs/`（纯 LangGraph：`context_schema` 注入、
> `stream_mode="custom"` 流式、原生 `retry_policy`/`timeout`、并行边归约），`app/agents/` 目录已删除。
> 架构说明见 `smartlife-agent/docs/graph-architecture.md`；本文其余章节描述的设计意图仍然有效。

## 十四、项目目录结构

```text
smartlife-agent/
├── app/
│   ├── main.py                         # Streamlit 入口
│   ├── classifier.py                   # 混合分类器与 ClassificationResult
│   ├── graphs/
│   │   ├── orchestrator.py             # 顶层图：分类、条件路由、复合分支、记忆提取
│   │   ├── context.py / events.py      # TurnContext 注入 + stream writer 事件出口
│   │   ├── state.py / nodes.py         # 图状态与通用节点
│   │   ├── memory.py                   # 记忆提取图 + 审批入口
│   │   └── subgraphs/                  # shopping / travel / trip_gear / composite /
│   │                                   # customer_service / general_plan / react / general_chat
│   ├── services/
│   │   ├── turn_services.py            # 无编排的领域能力（分类、检索、生成、记忆）
│   │   ├── travel_service.py           # 旅行 prompt、规划/审核调用、工具执行
│   │   └── task_models.py              # 共享约束与复合任务模型
│   ├── mcp_servers/
│   │   └── protocol_server.py          # 真实 MCP Protocol Server
│   ├── tools/
│   │   ├── agent_capabilities.py       # Agent Tool Policy 与越权拒绝
│   │   ├── registry.py                 # Actor/Agent 绑定工具注册表
│   │   ├── shopping_tools.py           # 电商工具
│   │   ├── travel_tools.py             # 旅游工具
│   │   ├── weather.py / map.py / time.py
│   │   └── payment.py                  # 兼容模块；支付工具已移除并返回空列表
│   ├── retrieval/
│   │   ├── service.py                  # SQL/RAG 检索编排
│   │   ├── nl2sql.py / rag.py / reranker.py
│   │   └── repositories.py             # 商品、评价和订单 Repository
│   ├── memory/                         # 短期、长期、Markdown 与审批
│   ├── evaluation/                     # RAG、Rerank、A/B 和 E2E 评测
│   └── observability.py                # Trace、耗时与模型用量
├── scripts/run_mcp_server.py           # 启动统一或 Agent 专属 MCP Server
├── docs/current-agent-flow.html        # 当前完整分支流程图
├── tests/                              # 单元、集成、MCP 和隔离测试
├── requirements.txt
└── requirements.lock
```

---

## 十五、关键建议总结

### 你要在面试中证明的事

1. **你理解每个技术的 trade-off**，不是只知道它是什么
2. **你有工程判断力**，知道什么时候该用、什么时候不该用
3. **你做过真实的评测**，有数据支撑你的设计决策
4. **你能回答"为什么不"**，不只是"为什么用"

### 最容易被追问的 5 个问题

| 问题 | 你要准备的深度 |
|------|-------------|
| 为什么不用纯 RAG 查商品？ | 结构化数据用 SQL 更精确，RAG 适合非结构化评价和攻略 |
| ReAct 和 Plan & Execute 怎么选？ | 小模型判断任务复杂度，简单任务走 ReAct 省成本，复杂任务走 Plan & Execute 保证质量 |
| MCP 和直接 function calling 有什么区别？ | MCP 是标准化工具协议，可独立发布能力面；当前主聊天仍使用静态 bind_tools，两者边界必须说清楚 |
| RAG 的粗排和精排怎么配合？ | Bi-Encoder 召回 20 条（快），Cross-Encoder 精排取 Top 5（准） |
| 结构化审核迭代几次？ | Travel 最多 3 次，GeneralPlan 最多 1 次，避免无限循环 |

### 一定要做的事

1. **跑通 LLM Judge RAG 评测**，有真实数字（Groundedness、Answer Relevancy 等）
2. **对比有 Rerank 和没有 Rerank 的效果**，用同一个评测集
3. **录一个 demo 视频**，展示完整流程（购物 → 记忆 → 旅游推荐）
4. **准备 3 个"我踩过的坑"**，比如 embedding 检索不准、LLM 输出格式不稳定、MCP 连接超时等

---

## 十六、技术栈最终清单

| 技术 | 用在哪 | 为什么用 |
|------|-------|---------|
| **LangChain** | 框架 | 统一的 Agent 编排框架 |
| **ReAct Agent** | 简单交互 | 快速响应，Thought-Action-Observation 循环 |
| **Plan & Execute Agent** | 复杂任务 | 多步分解，可回退，用户可审计划 |
| **结构化审核** | 结果自检 | 生成行程/清单后检查预算、天数和证据状态，不满意则重新规划 |
| **RAG** | 非结构化检索 | 用户评价、攻略、FAQ 的语义检索 |
| **Rerank + Top K** | RAG 精排 | Cross-Encoder 粗排后精排，提高检索质量 |
| **NL2SQL** | 结构化查询 | 自然语言到 SQL，精确筛选商品 |
| **MCP** | Agent 专属工具能力面 | tools/list 隔离、bind_tools 隔离、执行层越权拒绝 |
| **Function Calling** | 外部工具 | 天气、地图、时间和只读订单查询 |
| **JSON Schema** | 工具参数定义 | 保证输出结构化，减少解析错误 |
| **长期记忆** | 用户画像 | 跨会话偏好、跨场景推荐 |
