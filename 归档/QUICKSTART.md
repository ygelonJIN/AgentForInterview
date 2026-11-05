# 🚀 SmartLife Agent 快速启动指南

## 一、新 Mac 环境配置

### 1. 安装 Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 2. 安装 Python

```bash
# 安装 Python 3.12（推荐，稳定）
brew install python@3.12
```

### 3. 配置 PATH

在 `~/.zshrc` 中添加：

```bash
export PATH="/opt/homebrew/bin:$PATH"
```

然后运行：

```bash
source ~/.zshrc
python3 --version   # 应显示 3.12.x
pip3 --version
```

### 4. 配置 pip 镜像（可选，加速下载）

```bash
mkdir -p ~/.pip
cat > ~/.pip/pip.conf << 'CONF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
CONF
```

---

## 二、项目启动

### 方案 A：使用虚拟环境（推荐）

```bash
cd '/Volumes/TUF ESD-T1A Media/xagent/smartlife-agent'

# 创建并激活虚拟环境
python3 -m venv venv312
source venv312/bin/activate

# 安装已锁定依赖（可复现环境）
pip install -r requirements.lock -i https://pypi.tuna.tsinghua.edu.cn/simple

# 设置 API Key（可写入本地 data/config.json；该文件已忽略，不要提交 Git）
export OPENAI_API_KEY="your-api-key-here"
# 也可以分别设置：
# export SMARTLIFE_MAIN_API_KEY="..."
# export SMARTLIFE_SMALL_API_KEY="..."
# export SMARTLIFE_EMBEDDING_API_KEY="..."

# 首次创建带种子数据的数据库
python data/init_db.py

# 已有 products.db 时不要直接重建。确需重建时会先自动备份：
# python data/init_db.py --force
#
# 增量导入请使用网页“数据导入”或 scripts/import_data.py db

# 运行
streamlit run app/main.py
```

访问 http://localhost:8501

### 方案 B：使用简化版本（无需 LangChain）

```bash
pip3 install openai streamlit pydantic python-dotenv -i https://pypi.tuna.tsinghua.edu.cn/simple
streamlit run app/simple_main.py
```

### 运行测试

```bash
pytest tests/ -v
```

---

## 三、常见问题

### SSL 证书错误
```bash
pip3 install --trusted-host pypi.org --trusted-host files.pythonhosted.org <package>
```

### PyYAML 安装失败
```bash
pip3 install PyYAML --no-build-isolation
```

### 多个 Python 版本冲突
使用虚拟环境隔离，确保 `/opt/homebrew/bin` 在 PATH 最前面：

```bash
which python3    # 应指向 /opt/homebrew/bin/python3
python3 --version
```

### pip 安装很慢
确认已配置国内镜像源（见第一步第 4 节）。

### 执行日志与策略位置

智能助手回答下方会显示可展开的“执行日志”，记录：

- LangGraph 实际进入的条件分支；
- 每个节点的超时、最大尝试次数和实际尝试次数；
- 重试失败、重试安排、重试上限和最终失败节点；
- 旅行计划修订循环、行程步骤迭代次数和上限截断；
- 购物回答校验的一次修复流程；
- ReAct 工具循环步数、工具调用和达到上限后的收束；
- SQL/RAG 等数据源降级或部分成功情况。

### 路由行为速览

智能对话不是只靠一个语义模型判断：

```text
关键词快筛
  → 上下文/指代沿用
  → 小模型输出 intents + primary_intent + route + retrieval
  → confidence < 0.55 或格式异常时主模型仲裁
  → LangGraph 条件路由
```

- `intents` 决定使用哪个 Agent，最多保留两个；
- `route` 决定 `ReAct` 或 `Plan & Execute`；
- `retrieval` 决定 SQL、RAG 或混合检索；
- 明确的 `shopping + travel` 复合请求会并行执行两个 Agent，再合并正文；其他请求以 `primary_intent` 为主。

策略配置主要位于：

| 能力 | 代码位置 | 上限/策略 |
|------|----------|-----------|
| 熔断与 TTL 缓存 | `app/reliability.py` | 由检索/工具 provider 使用 |
| 节点超时与重试 | 各图 `add_node(..., retry_policy=, timeout=)` | 由 LangGraph 原生参数声明 |
| 顶层图 | `app/graphs/orchestrator.py` | 分类最多 2 次；场景节点通常 1 次 |
| 购物检索与回答修复 | `app/graphs/subgraphs/shopping.py` | 检索规划最多 2 次；回答修复 1 次；修复预算 120 秒 |
| 旅行生成、执行、审核、修订 | `app/graphs/subgraphs/travel.py` | 默认最多修订 3 次 |
| ReAct 工具推理循环 | `app/graphs/subgraphs/react.py` | 默认最多 6 步 |
| 日志事件格式和脱敏 | `app/execution_log.py` | API Key、Token 等字段自动脱敏 |

---

## 四、推荐的 Python 版本管理方式

| 方案 | 适合人群 | 命令 |
|------|---------|------|
| 只用 Homebrew | 新手 | `brew install python@3.12` |
| pyenv | 进阶用户 | `brew install pyenv && pyenv install 3.12.7` |
| conda | 数据科学 | `brew install --cask miniconda` |

---

## 五、功能演示

### 购物场景
```
用户：帮我找一双500块以内的跑步鞋
助手：我来帮您搜索合适的跑步鞋...
```

### 旅游场景
```
用户：我和女朋友周末去杭州玩两天，预算3000
助手：我来为您规划杭州两日游行程...
```

### 行程约束与装备协同
```
用户：周末两个人去莫干山露营，预算1500，我有帐篷和睡袋
助手：先给出约束清单和逐日行程，再给出装备清单（已有/待购/自备）、
      预算占用和未满足约束...
```

说明：这类请求会进入 `trip_gear` 链路；看到“商品库暂未匹配”表示商品库还没覆盖该品类，不是编造失败。

---

## 六、评测、A/B 与性能基线

### 1. 构建业务 Rerank 评测集

```bash
venv312/bin/python scripts/build_rerank_eval_from_db.py \
  --queries data/evaluation/query_log.example.jsonl \
  --db data/products.db \
  --output data/evaluation/rerank.business.jsonl
```

生产数据应使用真实用户查询日志，`relevant_product_ids` 由人工标注或业务点击/成交数据生成。

### 2. 运行 Cross-Encoder A/B

```bash
venv312/bin/python scripts/run_rerank_ab.py \
  --input data/evaluation/rerank.business.jsonl \
  --output data/evaluation/rerank.ab.json \
  --treatment cross_encoder
```

### 3. 运行端到端并发基线

```bash
# 默认使用免费且无需 API Key 的 Open-Meteo 天气和 OpenStreetMap 路线。
# 可选自定义 provider：
# export SMARTLIFE_WEATHER_API_URL="..."
# export SMARTLIFE_ROUTE_API_URL="..."
```

然后运行：

```bash
venv312/bin/python scripts/run_e2e_benchmark.py \
  --workload data/evaluation/e2e.workload.example.jsonl \
  --mode chat \
  --concurrency 4 \
  --output data/evaluation/e2e.baseline.json
```

报告会包含 P50/P95/P99、吞吐、成功率、Token、Provider 状态和 Trace 汇总。
`samples[].metadata.timing_ms` 区分首 Token、回答完成、回答后处理和完整链路耗时；
`metric_latency_ms` 按 `timing.*` 与 `node.<graph>:<node>` 聚合每一步的
P50/P95/P99，便于定位慢在检索、生成、校验还是后处理。

### 4. 启动真实 MCP Server

```bash
# 电商 Agent MCP（只包含电商工具）
venv312/bin/python scripts/run_mcp_server.py --transport stdio --agent shopping

# 旅游 Agent MCP（只包含旅游工具）
venv312/bin/python scripts/run_mcp_server.py --transport stdio --agent travel

# Streamable HTTP（建议生产环境配置 Bearer Token）
export SMARTLIFE_MCP_AUTH_TOKEN="replace-with-a-secret"
venv312/bin/python scripts/run_mcp_server.py \
  --transport streamable-http \
  --agent shopping \
  --host 127.0.0.1 \
  --port 8765
```

每个 `--agent` 都会启动独立 MCP 能力面，只发布该 Agent 自己的工具。工具隔离同时作用于 MCP 发现、模型 Tool Calling 和执行层；伪造的跨 Agent `tool_call` 不会真正执行。省略 `--agent` 时保留旧的统一 Tool Registry Server。

默认只发布只读工具；显式传入 `--include-write-tools` 时，记忆写入工具仍只创建审批请求。

### 5. 启用在线 Rerank A/B

```bash
export SMARTLIFE_AB_LOGGING=true
export SMARTLIFE_AB_SALT="replace-with-a-random-secret"
```

曝光会自动记录到 `data/evaluation/ab_events.db`；业务点击/转化通过 `app.evaluation.ab_logging.ExperimentLogger.log_outcome()` 记录，再用 `scripts/export_ab_ranking_cases.py` 导出可回放案例。
