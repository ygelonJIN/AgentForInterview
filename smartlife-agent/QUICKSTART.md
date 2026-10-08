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

策略配置主要位于：

| 能力 | 代码位置 | 上限/策略 |
|------|----------|-----------|
| 通用超时、重试、熔断、TTL 缓存 | `app/reliability.py` | 各调用方通过 `NodePolicy` 配置 |
| 顶层 LangGraph 节点 | `app/agents/langgraph_orchestrator.py` | 分类最多 2 次；场景节点通常 1 次 |
| 购物检索与回答修复 | `app/agents/shopping_graph.py` | 检索规划最多 2 次；回答修复 1 次；修复预算 120 秒 |
| 旅行生成、执行、审核、修订 | `app/agents/travel_graph.py` | 默认最多修订 3 次 |
| ReAct 工具推理循环 | `app/agents/react_graph.py` | 默认最多 6 步 |
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

报告会包含 P50/P95/P99、吞吐、成功率、Token、Provider 熔断/缓存状态和 Trace 汇总。

### 4. 启动真实 MCP Server

```bash
# stdio（供 MCP 客户端拉起）
venv312/bin/python scripts/run_mcp_server.py --transport stdio

# Streamable HTTP（建议生产环境配置 Bearer Token）
export SMARTLIFE_MCP_AUTH_TOKEN="replace-with-a-secret"
venv312/bin/python scripts/run_mcp_server.py \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8765
```

MCP Server 发布统一 Tool Registry 中的真实工具。默认只发布只读工具；显式传入 `--include-write-tools` 时，记忆写入工具仍只创建审批请求。

### 5. 启用在线 Rerank A/B

```bash
export SMARTLIFE_AB_LOGGING=true
export SMARTLIFE_AB_SALT="replace-with-a-random-secret"
```

曝光会自动记录到 `data/evaluation/ab_events.db`；业务点击/转化通过 `app.evaluation.ab_logging.ExperimentLogger.log_outcome()` 记录，再用 `scripts/export_ab_ranking_cases.py` 导出可回放案例。
