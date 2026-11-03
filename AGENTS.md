# xagent（AgentForInterview）

## 读取规则
- **做毕业设计相关的任何设计、实现、文档修改之前，先读 `不推送/毕业设计/课题定义.md`**，那是当前唯一口径。
- 改 Agent 编排、路由、RAG/NL2SQL、记忆、工具审批之前，先读 `README.md` 的架构和实现章节。
- 设计取舍看 `README.md` 的架构与实现章节；操作细节看 `经验文档/` 索引。
- 动代码之前先读 `smartlife-agent/QUICKSTART.md`。
- `不推送/` 只在明确需要时按文件名点读，**不做全目录扫描、不批量 grep**。
- `不推送/面试材料/` 是用户私有材料，**任何情况下都不读取**，只有用户本人人工查看。
- 索引里"什么时候读"有一条沾边，就读；判断不了就全读。

## 是什么
- 毕业设计：基于 LangGraph 的行程约束派生与装备协同规划智能体。方向是"一句需求 → 约束清单 + 逐日行程 + 装备清单 + 预算占用 + 未满足约束"，技术栈 LangGraph + LangChain + RAG/NL2SQL + 长期记忆 + Streamlit。
- 仓库根就是工作区根，代码在 `smartlife-agent/`，会话在这一层开。

## 目录
- `smartlife-agent/`：项目代码、测试、文档。
- `README.md`：项目说明与架构（已提交）。
- `project-design-v5.md`：历史设计稿 v5，**已过时，不读**，仅留档；现状以 `README.md` 为准。
- `不推送/`：私密材料，永不提交。当前有 `毕业设计/`、`面试材料/`、`发送包文档备份/`。
- `不推送/毕业设计/课题定义.md`：毕业设计的权威口径（题目、研究内容、技术指标、图结构、缺口清单、进度、提纲、文献），可读，动工前必读。
- `不推送/面试材料/`：面试复习、决策总表、开发记录、失败复盘，只有用户本人人工查看，助手不读取。
- `经验文档/`：可公开的操作经验，一领域一篇，索引见下。

## 经验索引

- `经验文档/Python环境与依赖.md`
  - 什么时候读：装包报 command not found / 新建项目要建环境 / 升级或回退依赖 / 换电脑或外接盘路径变了 / 怀疑有重复 venv / 要复现精确环境。
  - 包含什么：`python3 -m pip` 与裸 pip 的区别；一项目一 venv 的目录命名与忽略规则；venv 不可移动及盘符变化后的重建命令；`requirements.txt` 与 `requirements.lock` 的分工、lock 会过期；升级依赖的固定五步与回退；找出孤立环境的方法；pip/HF 缓存共享；给命令的单行与占位符约定。
  - 不用读：日常只装单个包且已在项目 venv 内。

- `经验文档/LangGraph本地调试与Studio.md`
  - 什么时候读：要起 LangGraph Studio / Agent Server / 改 `langgraph.json` / 图在 Studio 里报 checkpointer、context、KeyError、BlockingError / 要在本机直接跑图或换模型供应商。
  - 包含什么：应用界面、测试、Studio 三者的区别与各自启动命令；`langgraph-cli[inmem]` 与 `langgraph.json` 配置；接图的四条硬约束（零参工厂、上下文兜底、父图播种、装饰器误用）及对应报错；`--allow-blocking` 的成因；装 CLI 的降级风险与验收回退；换模型的三条单行命令与 embedding 注意事项；排错对照表。
  - 不用读：只想用应用界面聊天。

## 禁止
- 不提交 `不推送/` 里的任何文件。
- 不读取 `不推送/面试材料/` 下的任何文件。
- 不提交 `.env`、Key、`venv312/`、`chroma_db/`、`memory_db/` 等生成物。
- 不把经验文档写进 `smartlife-agent/` 的代码目录。
