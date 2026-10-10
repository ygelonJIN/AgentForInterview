# SmartLife Agent

SmartLife Agent 是一个面向电商导购、旅行规划、客服与通用任务的全栈智能体平台。项目基于 Python、Streamlit、LangChain、LangGraph、ChromaDB 和 MCP，演示了任务路由、智能体编排、混合检索、工具调用、长期记忆、审批机制、流式输出及评测能力。

> 项目代码位于 `smartlife-agent/` 目录。

## 功能特性

- **智能体编排**：根据任务类型自动选择 ReAct 或 Plan & Execute，并路由到购物、旅行、客服或通用规划智能体。
- **混合检索**：结合 NL2SQL、RAG、向量召回和 Cross-Encoder Rerank，分别处理结构化与非结构化数据。
- **工具调用**：统一 Tool Registry，支持天气、地图、时间、购物、旅行和记忆等工具。
- **MCP 协议服务**：支持 stdio 与 Streamable HTTP，可向外部 MCP 客户端动态发布工具。
- **长期记忆**：支持偏好、事件和对话记忆，提供 Markdown 同步、记忆压缩、用户隔离和记忆写入审批。
- **数据导入**：支持商品、订单、用户、评价等 CSV 数据，以及 PDF、DOCX、Excel 和 Markdown 文档导入。
- **可靠性机制**：包含超时、重试、熔断、缓存、节点执行策略和结果校验。
- **可观测性**：提供执行日志、步骤时间线、Trace、Token 统计和敏感信息脱敏。
- **评测体系**：支持 RAG 评测、Rerank A/B、端到端并发基准和性能基线校验。
- **敏感操作保护**：支付、记忆写入等敏感操作默认采用显式确认或审批机制。

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

```text
                         ┌──────────────────────┐
                         │    Streamlit Web UI   │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │  LangGraph Orchestrator│
                         └──────────┬───────────┘
                                    │
                  ┌─────────────────┼─────────────────┐
                  │                 │                 │
          ┌───────▼──────┐  ┌───────▼──────┐  ┌───────▼──────┐
          │ ReAct Agent  │  │ Plan&Execute │  │  Sub-Agents  │
          └───────┬──────┘  └───────┬──────┘  └───────┬──────┘
                  │                 │                 │
                  └─────────────────┼─────────────────┘
                                    │
               ┌────────────────────┼────────────────────┐
               │                    │                    │
       ┌───────▼──────┐    ┌────────▼────────┐   ┌──────▼──────┐
       │ Tool Registry │    │ Hybrid Retrieval │   │   Memory    │
       │  Tools / MCP  │    │ NL2SQL + RAG     │   │ Short / Long│
       └──────────────┘    └─────────────────┘   └─────────────┘
```

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

项目可以作为 MCP Server，将统一 Tool Registry 中的工具发布给外部 MCP 客户端。

### stdio 模式

```bash
venv312/bin/python scripts/run_mcp_server.py --transport stdio
```

### Streamable HTTP 模式

```bash
export SMARTLIFE_MCP_AUTH_TOKEN="replace-with-a-secret"

venv312/bin/python scripts/run_mcp_server.py \
  --transport streamable-http \
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
├── project-design-v5.md
├── smartlife-agent/
│   ├── app/
│   │   ├── agents/              # ReAct、Plan & Execute 和场景智能体
│   │   ├── evaluation/          # RAG、Rerank、A/B 与端到端评测
│   │   ├── memory/              # 短期记忆、长期记忆和记忆审批
│   │   ├── mcp_servers/         # MCP 协议服务
│   │   ├── retrieval/           # NL2SQL、RAG、规划与精排
│   │   ├── tools/               # 天气、地图、购物、旅行等工具
│   │   ├── ui/                  # Streamlit UI 组件
│   │   ├── config.py            # 模型配置
│   │   ├── main.py              # Web 应用入口
│   │   └── reliability.py       # 超时、重试、熔断和缓存
│   ├── data/                    # 示例数据、模板、数据库和评测数据
│   ├── docs/                    # 项目文档
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

## 延伸文档

- `project-design-v5.md`：完整系统设计、技术方案和实现说明。
- `smartlife-agent/QUICKSTART.md`：详细环境配置、故障排查和评测说明。
- `smartlife-agent/data/evaluation/README.md`：评测数据格式和运行方法。
- `smartlife-agent/data/import_templates/README.md`：数据导入格式说明。
