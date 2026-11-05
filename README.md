# SmartLife Agent

> **毕业设计题目**：基于 LangGraph 的行程约束派生与装备协同规划智能体设计与实现

SmartLife Agent 是一个面向电商导购、旅行规划、客服与通用任务的全栈智能体平台，也是本毕业设计的工程基础。项目基于 Python、Streamlit、LangChain、LangGraph、ChromaDB 和 MCP，演示了任务路由、智能体编排、混合检索、工具调用、长期记忆、审批机制、流式输出及评测能力。

本设计在通用平台之上聚焦一条纵向链路：把一句出行需求派生为可追溯的约束清单，生成结构化逐日行程，由行程推导装备清单，结合对话与长期记忆判断已有装备，只对缺口调用商品检索，最后做预算、时间与装备齐全性校验，并在有限轮次内修复。实现见 `smartlife-agent/app/trip_gear/`，说明见 `docs/architecture.md`。

> 项目代码位于 `smartlife-agent/` 目录。

## 功能特性

- **混合语义路由**：关键词快筛、上下文沿用、小模型语义分类和主模型仲裁共同判断意图；选择购物、旅游、客服或通用 Agent，并决定 ReAct / Plan & Execute。明确的电商+旅游复合任务会并行使用两个 Agent。
- **行程约束与装备协同**：把用户需求派生为带来源、硬度、状态的约束清单；生成逐日行程后推导装备清单，从对话/上文/长期记忆判断已有装备，只对缺口检索商品，并做预算、时间与装备齐全性校验。
- **纯 LangGraph 编排**：顶层图按 `intent` 选场景、按 `route` 选执行范式、按 `retrieval` 选 SQL/RAG/混合、按 `sub_intent=gear_plan` 走行程+装备链路。依赖注入走 `context_schema` + `Runtime.context`，流式走 `astream(stream_mode=["custom","tasks"])`，节点超时/重试走原生 `timeout` / `retry_policy`，复合意图用并行边 + reducer 归约。详见 `docs/architecture.md`。
- **混合检索**：结合 NL2SQL、RAG、向量召回和 Cross-Encoder Rerank，分别处理结构化与非结构化数据。
- **Agent 工具隔离**：电商、旅游和通用 Agent 拥有独立 Tool Registry，模型只能发现当前 Agent 的工具。
- **MCP 协议服务**：支持 stdio 与 Streamable HTTP，可把每个 Agent 作为独立 MCP 能力面即插即用；跨 Agent 工具在注册、发现和执行层都会被拒绝。
- **长期记忆**：支持偏好、事件和对话记忆，提供 Markdown 同步、记忆压缩、用户隔离和记忆写入审批。
- **数据导入**：支持商品、订单、用户、评价等 CSV 数据，以及 PDF、DOCX、Excel 和 Markdown 文档导入。
- **可靠性机制**：包含超时、重试、熔断、缓存、节点执行策略和结果校验。
- **可观测性**：提供执行日志、步骤时间线、Trace、Token 统计和敏感信息脱敏。
- **统一设计 token 与主题**：`app/ui/tokens.py` 用 94 个 token 管理颜色、字体、间距、形状与布局，`app/ui/theme.py` 的样式只消费 token，`.streamlit/config.toml` 对齐控件底座；行程与装备结果由 `plan_result` 结构化事件渲染成约束清单、逐日行程、装备清单、预算占用、校验结果五块面板，Markdown 原文默认收起可展开。
- **评测体系**：支持 RAG 评测、Rerank A/B、端到端并发基准和性能基线校验。
- **敏感操作保护**：支付、退款和下单不在当前能力范围；记忆写入默认采用显式确认或审批机制。

## 技术栈

| 领域 | 技术 |
|------|------|
| Web UI | Streamlit |
| 智能体编排 | LangGraph、LangChain |
| 模型接入 | OpenAI-compatible API |
| 向量检索 | ChromaDB |
| 精排 | sentence-transformers / Cross-Encoder |
| 协议服务 | MCP |
| 数据存储 | SQLite、Markdown |
| 可观测性 | OpenTelemetry |
| 数据处理 | Pandas、NumPy |
| 测试 | Pytest、Pytest-Asyncio |
| 文档解析 | pypdf、python-docx、openpyxl |

## 系统架构

一句话：UI 只驱动一张编译后的 LangGraph 图，业务能力（分类、检索、生成、记忆）下沉为无编排的服务层。

```text
Streamlit UI → prepare → classify（关键词/上下文/小模型/仲裁）
             → 条件边 ┬ shopping / travel / composite（并行）/ customer / general / react
                      └ trip_gear（sub_intent=gear_plan，装备类请求专线）
             → finalize →（可选）extract_memory
```

`classify` 输出四个信号，各管一件事：`intents` 选 Agent，`route` 选 ReAct 还是 Plan & Execute，`retrieval` 选 SQL/RAG/混合，`sub_intent=gear_plan` 进 `trip_gear`。多意图最多保留两个，只有 `shopping + travel` 走并行分支。

分层是 `app/main.py` → `app/graphs/orchestrator.py` → `app/graphs/subgraphs/*` → `app/services/*`；依赖注入、流式、重试/超时、节点计时、并发复合、人机中断全部用 LangGraph 原生能力。

完整全景图、五个原生约束、行程子系统（四态结论、代价、四类修复动作、规格匹配、采购可履约）与评测口径，见 **[`docs/architecture.md`](docs/architecture.md)**。

## 智能路由与多意图

当前不是“纯语义路由”，而是分层混合路由：

1. **关键词快筛**：明确商品、旅行、订单等词时零延迟分类。
2. **上下文沿用**：处理“改成手机”“再便宜一点”等指代和连续任务。
3. **小模型语义分类**：一次输出 `intents`、`primary_intent`、`route`、`retrieval`、`sub_intent` 和置信度。
4. **主模型仲裁**：置信度低于 `0.55` 或格式异常时仲裁/修复，最终失败才兜底为 `general`。

路由字段承担不同职责：

| 字段 | 作用 | 示例 |
|---|---|---|
| `intents` / `primary_intent` | 决定使用哪个 Agent | `shopping`、`travel` |
| `route` | 决定 ReAct 或 Plan & Execute | `react`、`plan_and_execute` |
| `retrieval` | 决定数据获取方式 | `sql_only`、`rag_only`、`mixed` |
| `sub_intent=gear_plan` | 决定是否进入行程+装备链路 | `gear_plan` → `trip_gear` |

多意图最多保留两个。只有同时包含 `shopping + travel` 时进入 `shopping_travel` 节点，两个 Agent 并行执行后合并回答；其他情况以 `primary_intent` 对应的 Agent 为主。

当消息同时包含出行语境和“装备/清单/带什么”，或露营、出差、度假等强场景时，分类器会置为 `travel + gear_plan`，优先进入 `trip_gear`；普通 `shopping + travel` 仍走并行分支。

## 快速开始

### 环境要求

- Python 3.12，推荐 3.12.x
- macOS 或 Linux
- 可用的 OpenAI-compatible API 服务
- 建议至少 4 GB 可用内存，以运行 Embedding 和 Rerank 模型

### 1. 获取项目

```bash
git clone https://github.com/ygelonJIN/AgentForInterview.git
cd AgentForInterview/smartlife-agent
```

### 2. 创建虚拟环境

```bash
python3.12 -m venv venv312
source venv312/bin/activate
```

### 3. 安装依赖

推荐使用锁定依赖，以获得可复现环境：

```bash
pip install -r requirements.lock
```

也可以安装非锁定版本：

```bash
pip install -r requirements.txt
```

国内网络慢时可以换镜像源：

```bash
pip install -r requirements.lock -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 4. 初始化示例数据库

```bash
python data/init_db.py
```

已有 `data/products.db` 时请勿重复初始化。确需重建时可执行：

```bash
python data/init_db.py --force
```

### 5. 配置模型

可以通过环境变量配置 API Key：

```bash
export OPENAI_API_KEY="your-api-key"
```

也可以为不同模型分别配置：

```bash
export SMARTLIFE_MAIN_API_KEY="your-main-model-api-key"
export SMARTLIFE_SMALL_API_KEY="your-small-model-api-key"
export SMARTLIFE_EMBEDDING_API_KEY="your-embedding-api-key"
```

模型地址和模型名称可通过 Web UI 的配置页面填写，或复制模板后进行配置：

```bash
cp data/config.example.json data/config.json
```

配置示例：

```json
{
  "main": {
    "base_url": "https://your-provider.example/v1",
    "model": "your-chat-model"
  },
  "small": {
    "base_url": "https://your-provider.example/v1",
    "model": "your-small-model"
  },
  "embedding": {
    "base_url": "https://your-provider.example/v1",
    "model": "your-embedding-model"
  }
}
```

API Key 不写入公开配置文件。通过 UI 保存的密钥存放在 `data/config.secrets.json`，该文件具有本地权限保护且不应提交到 Git。

### 6. 启动应用

```bash
./run.sh
```

或手动启动：

```bash
streamlit run app/main.py
```

浏览器访问：

```text
http://localhost:8501
```

### 方案 B：简化版（无需 LangChain）

只跑一个最小可用界面时：

```bash
pip install openai streamlit pydantic python-dotenv
streamlit run app/simple_main.py
```

## 主要场景

### 智能导购

- 使用自然语言筛选商品。
- 按品牌、价格、分类和属性推荐商品。
- 查询商品详情、评价和销售数据。
- 比较多个商品并生成购买建议。

示例：

```text
帮我找一双 500 元以内的跑步鞋，最好缓震好一点。
```

### 旅行规划

- 查询天气、目的地信息和路线。
- 根据人数、预算、天数和偏好生成行程。
- 计算预算并自动修订超支方案。
- 输出可执行的逐日行程和费用估算。

示例：

```text
我和朋友周末去杭州玩两天，预算 3000 元，帮我规划一个轻松一点的行程。
```

### 行程约束与装备协同

- 从一句话派生约束清单（目的地/天数/人数/预算/住宿/天气），并标注来源。
- 生成逐日行程，再由行程推导装备清单；装备不限于户外，裙装、正装、儿童用品同样处理。
- 从当前对话、上文用户发言和长期记忆判断“已有装备”，只补缺口。
- 预算默认含装备；超预算时有限轮次内自动削减可选装备。
- 商品库暂未覆盖的装备会如实标注，不编造价格。

示例：

```text
周末两个人去莫干山露营，预算 1500，我有帐篷和睡袋。
```

说明见 [`docs/architecture.md`](docs/architecture.md)。

### 客服与通用规划

- 回答订单和售后问题。
- 将复杂任务分解为多个执行步骤。
- 根据执行结果进行反思、校验和修复。
- 在数据不足时选择 SQL、RAG 或外部工具，并记录降级过程。

## 混合检索

SmartLife Agent 将数据源划分为两类：

- **结构化数据**：商品、订单、用户和评价等存放在 SQLite 中，通过 NL2SQL 查询。
- **非结构化数据**：旅游攻略、活动说明、商品评价和导入文档通过 RAG 检索。

典型检索流程：

```text
用户问题
   │
   ├─ 识别数据意图
   │
   ├─ 结构化意图 ──> SQL 安全检查 ──> SQLite 查询
   │
   └─ 非结构化意图 ──> 向量召回 ──> Cross-Encoder 精排 ──> Top K
                                      │
                                      └─> 与结构化结果合并
```

## 智能体执行模式

### ReAct

适用于需要多轮观察和工具调用的购物、客服和开放式任务。

```text
思考 -> 选择工具 -> 执行 -> 观察结果 -> 继续推理 -> 最终回答
```

### Plan & Execute

适用于旅行规划、采购清单等需要分步骤完成的复杂任务。

```text
任务分析
   │
   ├─ 生成执行计划
   ├─ 逐步执行
   ├─ 汇总执行结果
   ├─ 结构化审核
   └─ 必要时重新规划或修订
```

## MCP Server

项目可以将每个 Agent 作为独立 MCP Server 发布。每个 Server 使用自己的能力清单，不混用其他 Agent 的工具：

| Agent | 独占工具 |
|---|---|
| `shopping` | `search_products`、`get_product_reviews`、`get_order_status`（订单需要 Actor） |
| `travel` | `search_destinations`、`get_local_activities`、`plan_itinerary`、`get_weather`、`get_route`、`get_current_time` |
| `general` | `get_weather`、`get_route`、`get_current_time` |

`shopping` 与 `travel` 的工具集合严格互斥。

这种结构支持即插即用：外部 MCP 客户端只连接所需 Agent；即使模型被注入并伪造跨域 `tool_call`，运行时也会拒绝。当前主聊天由 LangGraph 直接使用进程内 Agent Registry；远端 MCP Agent 的自动注册、发现和消息路由仍是后续目标。

### 当前实现边界

当前“即插即用”是指外部 MCP 客户端可以独立连接、替换或升级某个 Agent MCP Server，并且只能看到和调用该 Agent 自己的工具。

已经实现：

- 按 `--agent shopping/travel/general` 启动独立 MCP Server；
- MCP `tools/list` 只返回当前 Agent 的工具；
- 模型 `bind_tools()` 只绑定当前 Agent 的工具；
- 执行层拒绝伪造或越权的跨 Agent `tool_call`。

尚未实现：

- 远端 MCP Agent 的自动注册和动态发现；
- Orchestrator 通过 MCP 网络协议路由聊天消息；
- 多个远端 Agent 之间的能力协商。

当前主聊天链路仍由 LangGraph 使用进程内 Agent Registry 和静态 `bind_tools()` 执行，不依赖 MCP 网络调用。

### stdio 模式

```bash
# 电商 Agent MCP
venv312/bin/python scripts/run_mcp_server.py --transport stdio --agent shopping

# 旅游 Agent MCP
venv312/bin/python scripts/run_mcp_server.py --transport stdio --agent travel
```

不传 `--agent` 时保留旧的统一 Tool Registry Server，便于兼容已有客户端。

### Streamable HTTP 模式

```bash
export SMARTLIFE_MCP_AUTH_TOKEN="replace-with-a-secret"

venv312/bin/python scripts/run_mcp_server.py \
  --transport streamable-http \
  --agent shopping \
  --host 127.0.0.1 \
  --port 8765
```

默认只发布只读工具。需要包含写入类工具时：

```bash
venv312/bin/python scripts/run_mcp_server.py \
  --transport stdio \
  --include-write-tools
```

即使启用写入工具，记忆写入仍会先创建审批请求，不会直接修改长期记忆。

## 数据导入

可以通过 Web UI 的“数据导入”页面导入：

- 商品、订单、用户和评价 CSV。
- PDF、DOCX、Excel、Markdown 和 TXT 文档。
- RAG 指南、活动和用户评价内容。
- 数据库结构化数据与向量知识库内容。

模板位于：

```text
smartlife-agent/data/import_templates/
```

命令行增量导入：

```bash
venv312/bin/python scripts/import_data.py db
```

## 测试

运行完整测试：

```bash
pytest tests/ -v
```

运行安全检查：

```bash
venv312/bin/python scripts/security_check.py
```

验证外部工具：

```bash
venv312/bin/python scripts/verify_external_tools.py
```

## 性能评测

### 构建 Rerank 业务评测集

```bash
venv312/bin/python scripts/build_rerank_eval_from_db.py \
  --queries data/evaluation/query_log.example.jsonl \
  --db data/products.db \
  --output data/evaluation/rerank.business.jsonl
```

### 运行 Rerank A/B

```bash
venv312/bin/python scripts/run_rerank_ab.py \
  --input data/evaluation/rerank.business.jsonl \
  --output data/evaluation/rerank.ab.json \
  --treatment cross_encoder
```

### 运行端到端并发基准

```bash
venv312/bin/python scripts/run_e2e_benchmark.py \
  --workload data/evaluation/e2e.workload.example.jsonl \
  --mode chat \
  --concurrency 4 \
  --output data/evaluation/e2e.baseline.json
```

评测报告包含：

- P50、P95、P99 延迟。
- 吞吐量与成功率。
- Token 使用量。
- Provider 状态。
- 首 Token、生成、后处理和完整链路耗时。
- 各 LangGraph 节点的性能分解。

## 目录结构

```text
.
├── docs/
│   ├── architecture.md          # 唯一的实现说明
│   └── generated/               # 生成物（流程图、全链路规格），勿手改
├── 经验文档/                    # 可公开的操作经验，一领域一篇
├── 归档/                        # 历史文档，不代表现状
├── smartlife-agent/
│   ├── .streamlit/              # Streamlit 主题配置（与 design token 对齐）
│   ├── app/
│   │   ├── agents/              # ReAct、Plan & Execute 和场景智能体
│   │   ├── evaluation/          # RAG、Rerank、A/B 与端到端评测
│   │   ├── memory/              # 短期记忆、长期记忆和记忆审批
│   │   ├── mcp_servers/         # MCP 协议服务
│   │   ├── retrieval/           # NL2SQL、RAG、规划与精排
│   │   ├── tools/               # 天气、地图、购物、旅行等工具
│   │   ├── trip_gear/           # 约束派生、逐日行程、装备推导与校验
│   │   ├── ui/                  # Streamlit UI 组件
│   │   ├── config.py            # 模型配置
│   │   ├── main.py              # Web 应用入口
│   │   └── reliability.py       # 超时、重试、熔断和缓存
│   ├── data/                    # 示例数据、模板、数据库和评测数据
│   ├── memory_docs/             # 本地记忆 Markdown
│   ├── scripts/                 # 导入、评测、MCP 和检查脚本
│   ├── tests/                   # 自动化测试
│   ├── requirements.txt
│   ├── requirements.lock
│   └── run.sh
└── README.md
```

## 配置与安全建议

- 不要提交 `data/config.json` 和 `data/config.secrets.json`。
- 不要在代码、日志或 Git 提交中保存 API Key。
- MCP Streamable HTTP 模式应配置 `SMARTLIFE_MCP_AUTH_TOKEN`。
- 生产环境建议绑定 `127.0.0.1`，并通过反向代理提供 HTTPS。
- 写入类工具默认关闭，只有明确传入 `--include-write-tools` 时才发布。
- 支付、退款和记忆修改等敏感操作应保留审批流程。
- 部署前执行 `scripts/security_check.py`。

## 常见问题

### Streamlit 启动后无法访问

确认服务已启动，并访问：

```text
http://localhost:8501
```

检查端口占用：

```bash
lsof -i :8501
```

### 模型调用失败

依次检查：

1. API Key 是否有效。
2. `base_url` 是否包含正确的 API 地址。
3. 模型名称是否正确。
4. 网络或代理是否可以访问模型服务。
5. `main`、`small`、`embedding` 三个配置是否均已设置。

### Rerank 模型首次运行较慢

Cross-Encoder 首次运行需要下载模型。模型下载完成后会缓存，后续启动速度会明显提高。

### 无法找到商品数据库

确认当前目录为 `smartlife-agent/`，并检查：

```text
smartlife-agent/data/products.db
```

如数据库不存在，可运行：

```bash
python data/init_db.py
```

### SSL 证书错误

```bash
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org <package>
```

### PyYAML 安装失败

```bash
pip install PyYAML --no-build-isolation
```

### 多个 Python 版本冲突

用虚拟环境隔离，并确保虚拟环境已激活（`which python` 应指向 `venv312/bin/python`）。

## 延伸文档

- `docs/architecture.md`：**唯一的实现说明**——分层、顶层图、行程子系统（四态结论、代价、四类修复动作、规格匹配、采购可履约）、评测口径、文件速查。
- `经验文档/`：可公开的操作经验，一领域一篇（环境依赖、LangGraph 调试、行程子系统与评测）。
- `归档/`：历史文档（含旧版设计稿与已合并的子系统文档），**只作查证用，不作为实现依据**。
- `smartlife-agent/data/import_templates/README.md`：数据导入格式说明。
