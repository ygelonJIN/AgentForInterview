# SmartLife Agent — 全栈智能体电商旅游平台 v5

> **文档状态（2026-10-05）**
> - Current：`smartlife-agent/app/main.py -> LangGraphOrchestrator -> OrchestratorV2` 是当前入口。
> - Target：MCP 动态发现、外部天气/酒店/路线服务、完整跨场景采购联动仍是目标能力。
> - Deprecated：`app/agents/orchestrator.py` 是旧兼容实现，不属于当前主链路。
> - Removed/Unwired：旧的 MCP 动态发现和未接线组件不得被视为已完成功能。

## 项目定位

> 基于 LangChain 构建的 Multi-Agent 智能体系统，统一覆盖**电商购物**和**旅游出行**两大场景。
> 核心卖点不是"两个场景拼在一起"，而是**共享记忆、跨场景推荐、社交协商**三个只有合并才能成立的能力。

---

## 一、为什么合并？——三个不可拆分的理由

| 理由 | 具体场景 | 为什么拆不开 |
|------|---------|-------------|
| **记忆驱动的跨场景推荐** | 用户上周买了帐篷和登山杖，这周问"周末去哪玩" | 记忆存在同一个向量库里，旅游 Agent 能直接召回购物场景的语义，推荐露营路线 |
| **社交协商的跨域偏好** | 三个朋友一起出行，A 买了烧烤架，B 买了飞盘 | 协商 Agent 需要同时读取每个人的购物偏好和旅行偏好，才能找到交集 |
| **统一预算与采购联动** | 用户说"我要露营，帮我列装备清单+行程" | 购物清单和行程规划需要同步预算，由同一个 Plan & Execute Agent 统筹 |

---

## 二、技术栈与映射

| 技术 | 在项目中的真实用途 | 面试深度切入点 |
|------|------------------|--------------|
| **ReAct Agent** | 主推理引擎，处理大多数交互式对话和导购 | ReAct 的 Thought-Action-Observation 循环、与 CoT 的区别 |
| **Plan & Execute Agent** | 多步复杂任务规划（行程规划、预算分配、采购清单） | Plan 的分解策略、Execute 的回退机制、与 ReAct 的选择标准 |
| **Reflection Agent** | 复杂任务结果的自检和优化 | 生成行程/清单后自检合理性，不满意则重新规划 |
| **RAG + Rerank + Top K** | 非结构化内容检索（用户评价、攻略、FAQ） | 粗排 + 精排的设计、Cross-Encoder vs Bi-Encoder 的 trade-off |
| **NL2SQL** | 结构化商品查询（价格、分类、库存） | 自然语言到 SQL 的准确性、与 RAG 的分工 |
| **MCP** | 子 Agent 工具暴露，Orchestrator 动态发现 | MCP 协议设计、与直接 function calling 的区别、Server 注册机制 |
| **Function Calling** | 天气、地图、支付、时间等外部工具调用 | 工具描述设计、参数约束、错误处理 |
| **JSON Schema** | 工具参数定义、LLM 输出格式约束 | 保证输出结构化，减少解析错误，与 Pydantic 的配合 |
| **长期记忆** | 用户画像 + 跨会话摘要记忆 + 跨场景推荐 | 摘要压缩策略、检索质量保证、记忆淘汰机制 |
| **社交协商** | 多人出行/购物的偏好协调 | 状态机编排、偏好权重、冲突解决策略 |

---

## 三、系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                     用户入口 (Streamlit)                              │
│           购物场景 | 旅游场景 | 社交协商 | 个人中心                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  任务路由器 (小模型)   │
                    │  判断简单/复杂任务     │
                    │  → ReAct / Plan&Exec │
                    └──────────┬──────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────┐
│               Orchestrator Agent (ReAct)                             │
│         意图识别 → 通过 MCP 动态发现子 Agent → 路由                     │
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ Plan &       │  │ Shopping     │  │    Travel Agent          │  │
│  │ Execute      │  │ Agent        │  │    (行程+活动+攻略)       │  │
│  │ (预算+行程)   │  │ (导购+客服)   │  │                          │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────────┘  │  │
│         │                 │                    │                │  │
│         └────────┬────────┴────────────────────┘                │  │
│                  │                                               │  │
│  ┌───────────────▼──────────────────────────────────────────┐   │  │
│  │              MCP 协议层                                    │   │  │
│  │  Shopping Agent → MCP Server (商品查询/筛选/下单 tools)     │   │  │
│  │  Travel Agent   → MCP Server (景点/活动/天气 tools)        │   │  │
│  │  Memory Agent   → MCP Server (记忆读写 tools)             │   │  │
│  └──────────────────────────────────────────────────────────┘   │  │
│                                                                  │  │
│  ┌──────────────────┐  ┌──────────────────┐                     │  │
│  │  混合检索层        │  │  长期记忆层        │                     │  │
│  │  ┌─────────────┐ │  │  用户画像          │                     │  │
│  │  │ NL2SQL      │ │  │  跨会话摘要        │                     │  │
│  │  │ (结构化查询)  │ │  │  跨场景偏好        │                     │  │
│  │  ├─────────────┤ │  └──────────────────┘                     │  │
│  │  │ RAG+Rerank  │ │                                           │  │
│  │  │ (语义检索)   │ │  ┌──────────────────┐                     │  │
│  │  ├─────────────┤ │  │  社交协商层        │                     │  │
│  │  │ 结果融合     │ │  │  偏好分析          │                     │  │
│  │  │ + Top K     │ │  │  冲突解决          │                     │  │
│  │  └─────────────┘ │  │  妥协方案          │                     │  │
│  └──────────────────┘  └──────────────────┘                     │  │
└──────────────────────────────────────────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Streamlit 前端     │
                    │  对话 | 筛选 | 地图   │
                    └─────────────────────┘
```

---

## 四、任务路由：小模型判断 ReAct vs Plan & Execute

用一个轻量级模型（如 GPT-4o-mini 或本地小模型）做任务分类，而不是每次都用大模型判断。

### 路由规则

| 判断条件 | ReAct | Plan & Execute |
|---------|-------|----------------|
| 步骤之间有依赖关系？ | 单步或简单多步 | 后续步骤依赖前面结果 |
| 需要用户中间确认？ | 直接给结果 | 先出计划让用户审 |
| 任务可分解为独立子任务？ | 否 | 是 |
| 需要回退和重新规划？ | 否 | 是 |

### 路由实现

```python
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

class TaskRoute(BaseModel):
    """任务路由结果"""
    route: str = Field(description="react 或 plan_and_execute")
    reason: str = Field(description="路由理由")

router_prompt = ChatPromptTemplate.from_messages([
    ("system", """你是一个任务路由器。判断用户请求应该用哪种 Agent 处理。

ReAct Agent 适合：
- 单次查询（商品搜索、天气查询、简单问答）
- 导购对话（帮我找XX）
- 客服问答（退换货政策、订单状态）

Plan & Execute Agent 适合：
- 多步规划（行程规划、采购清单、预算分配）
- 跨场景任务（买装备+规划旅行）
- 需要用户确认计划的任务

关键词提示：规划、计划、清单、安排、预算分配、帮我列、行程 → 复杂任务"""),
    ("user", "{user_message}")
])

# 用小模型做路由，成本低、速度快
router_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
router = router_prompt | router_llm.with_structured_output(TaskRoute)

def route_task(user_message: str) -> TaskRoute:
    result = router.invoke({"user_message": user_message})
    return result
```

### 具体场景分配

**ReAct 处理：**
- "帮我找一双 500 块以内的跑步鞋" → 搜索、筛选、返回结果
- "这个商品退换货政策是什么" → 查知识库、回答
- "杭州明天天气怎么样" → 调工具、返回
- 简单导购对话、客服问答

**Plan & Execute 处理：**
- "我要装修新房，预算 5 万，帮我列采购清单"
  → Plan: 1.确定房间 2.列品类 3.每个品类选品 4.算总价 5.调整预算
- "我和女朋友周末去杭州玩两天，预算 3000"
  → Plan: 1.查天气 2.查景点 3.查活动 4.排日程 5.算费用
- "帮我规划一个露营装备清单+周末露营行程"
  → 跨场景，需要先规划再执行

---

## 五、MCP 协议：子 Agent 工具暴露与动态发现

### 为什么用 MCP 而不是硬编码 tool list？

硬编码方式：Orchestrator 里写死 shopping_tools = [search_products, filter_products, ...]，新增工具要改 Orchestrator 代码。

MCP 方式：每个子 Agent 暴露为 MCP Server，Orchestrator 通过 MCP 协议动态查询"你现在有什么能力"，然后决定调用哪个。

### MCP Server 设计

```python
# Shopping Agent MCP Server
from mcp.server import Server
from mcp.types import Tool

shopping_server = Server("shopping-agent")

@shopping_server.tool()
def search_products(query: str, category: str = None, max_price: float = None) -> list:
    """搜索商品，支持自然语言描述和结构化筛选"""
    # NL2SQL 处理结构化条件
    # RAG 处理语义描述
    ...

@shopping_server.tool()
def get_product_reviews(product_id: str, aspect: str = None) -> list:
    """获取商品评价，支持按方面筛选"""
    ...

@shopping_server.tool()
def place_order(user_id: str, product_id: str, quantity: int) -> dict:
    """下单"""
    ...

# Travel Agent MCP Server
travel_server = Server("travel-agent")

@travel_server.tool()
def search_destinations(preference: str, budget: float, days: int) -> list:
    """搜索目的地，根据偏好和预算推荐"""
    ...

@travel_server.tool()
def get_local_activities(city: str, date: str, interests: list = None) -> list:
    """获取本地活动信息"""
    ...

@travel_server.tool()
def plan_itinerary(destination: str, days: int, budget: float, interests: list) -> dict:
    """规划行程"""
    ...

# Memory Agent MCP Server
memory_server = Server("memory-agent")

@memory_server.tool()
def get_user_profile(user_id: str) -> dict:
    """获取用户画像"""
    ...

@memory_server.tool()
def save_preference(user_id: str, preference: dict) -> dict:
    """保存用户偏好"""
    ...

@memory_server.tool()
def get_cross_scene_memories(user_id: str, current_scene: str) -> list:
    """获取跨场景记忆（购物到旅游 / 旅游到购物）"""
    ...
```

### Orchestrator 通过 MCP 动态发现

```python
from langchain_mcp import MCPClient

async def discover_tools():
    """Orchestrator 动态发现所有子 Agent 的工具"""
    async with MCPClient() as client:
        # 连接所有 MCP Server
        await client.connect("shopping-agent", "localhost:8001")
        await client.connect("travel-agent", "localhost:8002")
        await client.connect("memory-agent", "localhost:8003")
        
        # 动态获取所有可用工具
        tools = await client.list_tools()
        return tools  # 自动转为 LangChain Tool 对象
```

### 面试话术

"我把每个子 Agent 封装成 MCP Server，Orchestrator 通过 MCP 协议动态发现和调用子 Agent 的工具，而不是硬编码路由表。这样新增一个子 Agent 不需要改 Orchestrator 的代码，只需要启动一个新的 MCP Server 并注册即可。"

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

## 九、Reflection Agent：结果自检与优化

### 为什么需要 Reflection？

Plan & Execute 生成的行程/清单可能有不合理的地方（行程太赶、预算超支、时间冲突）。Reflection Agent 负责自检和优化。

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

"Plan & Execute 生成的计划可能有不合理的地方，比如行程太赶或预算超支。Reflection Agent 会自检这些问题，如果有问题就用改进建议重新生成计划，最多迭代3次。这样保证了最终输出的质量。"

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

## 十二、社交协商系统

### 场景

三个朋友一起出行：
- A：喜欢户外运动，买了帐篷
- B：喜欢美食，预算有限
- C：喜欢拍照打卡

需要找到三人的交集。

### 实现

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict

class NegotiationState(TypedDict):
    """协商状态"""
    participants: list[dict]
    preferences: dict
    conflicts: list[dict]
    compromise: dict
    status: str

def analyze_preferences(state: NegotiationState) -> NegotiationState:
    """分析每个参与者的偏好"""
    preferences = {}
    for participant in state["participants"]:
        for key, value in participant["preferences"].items():
            if key not in preferences:
                preferences[key] = []
            preferences[key].append(value)
    state["preferences"] = preferences
    return state

def detect_conflicts(state: NegotiationState) -> NegotiationState:
    """检测冲突"""
    conflicts = []
    for key, values in state["preferences"].items():
        if len(set(values)) > 1:
            conflicts.append({"field": key, "options": list(set(values))})
    state["conflicts"] = conflicts
    return state

def resolve_conflicts(state: NegotiationState) -> NegotiationState:
    """解决冲突，生成妥协方案"""
    compromise = {}
    for conflict in state["conflicts"]:
        resolution = llm.invoke(f"如何在{conflict['options']}之间找到折中？")
        compromise[conflict["field"]] = resolution
    state["compromise"] = compromise
    state["status"] = "resolved"
    return state

# 构建协商状态机
workflow = StateGraph(NegotiationState)
workflow.add_node("analyze", analyze_preferences)
workflow.add_node("detect", detect_conflicts)
workflow.add_node("resolve", resolve_conflicts)

workflow.add_edge("analyze", "detect")
workflow.add_edge("detect", "resolve")
workflow.add_edge("resolve", END)

workflow.set_entry_point("analyze")
negotiation_agent = workflow.compile()
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

## 十四、项目目录结构

```
smartlife-agent/
├── app/
│   ├── main.py                    # Streamlit 入口
│   ├── router.py                  # 任务路由器（小模型分类）
│   ├── agents/
│   │   ├── orchestrator.py        # 主 Orchestrator (ReAct)
│   │   ├── shopping_agent.py      # 导购+客服 Agent
│   │   ├── travel_agent.py        # 旅游 Agent (Plan & Execute)
│   │   ├── negotiation_agent.py   # 社交协商 Agent
│   │   └── reflection.py          # Reflection Agent
│   ├── mcp_servers/
│   │   ├── shopping_server.py     # Shopping MCP Server
│   │   ├── travel_server.py       # Travel MCP Server
│   │   └── memory_server.py       # Memory MCP Server
│   ├── tools/
│   │   ├── weather.py             # 天气 Function Calling
│   │   ├── map.py                 # 地图 Function Calling
│   │   ├── payment.py             # 支付 Function Calling
│   │   └── time.py                # 时间 Function Calling
│   ├── retrieval/
│   │   ├── nl2sql.py              # NL2SQL 模块
│   │   ├── rag.py                 # RAG 检索模块
│   │   ├── reranker.py            # Cross-Encoder Rerank
│   │   └── fusion.py              # 结果融合
│   ├── memory/
│   │   ├── short_term.py          # 短期记忆（会话）
│   │   ├── long_term.py           # 长期记忆（向量库）
│   │   └── compressor.py          # 摘要压缩
│   ├── negotiation/
│   │   ├── graph.py               # LangGraph 状态机
│   │   ├── preference.py          # 偏好分析
│   │   └── conflict.py            # 冲突解决
│   └── evaluation/
│       ├── ragas_eval.py          # RAGAS 评测
│       └── test_cases.py          # 测试用例
├── data/
│   ├── products.db                # 商品数据库
│   ├── reviews/                   # 用户评价（向量化）
│   ├── guides/                    # 旅游攻略（向量化）
│   └── activities/                # 本地活动
├── tests/
│   ├── test_rag.py                # RAG 评测
│   ├── test_nl2sql.py             # NL2SQL 测试
│   ├── test_agents.py             # Agent 测试
│   └── test_negotiation.py        # 协商测试
├── requirements.txt
└── README.md
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
| MCP 和直接 function calling 有什么区别？ | MCP 支持动态发现和跨 Agent 共享，function calling 是静态绑定 |
| RAG 的粗排和精排怎么配合？ | Bi-Encoder 召回 20 条（快），Cross-Encoder 精排取 Top 5（准） |
| Reflection Agent 迭代几次？ | 最多 3 次，避免无限循环，每次用改进建议重新生成 |

### 一定要做的事

1. **跑通 RAGAS 评测**，有真实数字（Faithfulness、Answer Relevancy 等）
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
| **Reflection Agent** | 结果自检 | 生成行程/清单后自检合理性，不满意则重新规划 |
| **RAG** | 非结构化检索 | 用户评价、攻略、FAQ 的语义检索 |
| **Rerank + Top K** | RAG 精排 | Cross-Encoder 粗排后精排，提高检索质量 |
| **NL2SQL** | 结构化查询 | 自然语言到 SQL，精确筛选商品 |
| **MCP** | 子 Agent 工具暴露 | Orchestrator 动态发现子 Agent 能力 |
| **Function Calling** | 外部工具 | 天气、地图、支付、时间等简单工具 |
| **JSON Schema** | 工具参数定义 | 保证输出结构化，减少解析错误 |
| **长期记忆** | 用户画像 | 跨会话偏好、跨场景推荐 |
| **社交协商** | 多人协调 | 偏好分析、冲突解决、妥协方案 |
