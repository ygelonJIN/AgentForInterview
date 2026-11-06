# 架构与实现

改代码前看这一份。目录约定、分层、顶层图、行程子系统、评测口径都在这里；
可公开的操作经验在 `经验文档/`，历史文档在 `归档/`。

## 全景

```text
                    ┌──────────────────────┐
                    │   Streamlit Web UI    │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  prepare（建 thread）  │
                    └──────────┬───────────┘
                    ┌──────────▼───────────┐
                    │ classify（一次小模型） │  关键词 → 上下文 → 模型 → 仲裁
                    │ intents/route/retrieval│
                    └──────────┬───────────┘
                    ┌──────────▼───────────┐
                    │ 条件边（原生）         │
                    └─┬────┬────┬────┬────┬─┘
        ┌─────────────▼┐ ┌─▼──────┐ ┌▼─────────────┐ ┌▼──────────────┐
        │ shopping     │ │ travel │ │ composite     │ │ customer /    │
        │              │ │        │ │ （并行合并）   │ │ general / react│
        └──────┬───────┘ └──┬─────┘ └──────┬────────┘ └──────┬────────┘
               └────────────┴──────────────┴─────────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │ finalize →（可选）extract_memory│
                    └───────────────────────────────┘

能力面（节点内调用，不持有编排）：Tool Registry / MCP、Hybrid Retrieval（NL2SQL+RAG+Rank）、Memory（短/长/MD/审批）
```

装备类请求（`sub_intent=gear_plan`）不进并行分支，走 `trip_gear` 子图，见下文「行程子系统」。

分层：`app/main.py`（UI，只调 TurnGraph）→ `app/graphs/orchestrator.py`（`build_turn_graph()`）→ `app/graphs/subgraphs/*`（节点签名统一 `(state, runtime)`）→ `app/services/*`（无编排的领域能力）→ `app/retrieval` / `app/memory` / `app/tools`。

## 五个"原生"约束

| 维度 | 原生做法 | 已废弃做法 |
| --- | --- | --- |
| 依赖注入 | `context_schema=TurnContext` + `runtime.context` | 编排器实例属性 + `config["configurable"]` |
| 流式 | `runtime.stream_writer(...)` + `astream(stream_mode="custom", subgraphs=True)` | 自研 `EventQueue` |
| 重试/超时 | `add_node(..., retry_policy=..., timeout=...)` | 自研 `run_policy_node` |
| 节点计时 | `stream_mode="tasks"` 的 `node_started` / `node_succeeded` | 包装器里手写日志 |
| 并发复合 | 并行边 + `branch_results` reducer | 节点里 `asyncio.gather` |
| 人机中断 | `interrupt()` + `Command(resume=...)` + checkpointer | 直接调审批对象方法 |

## 顶层路由

`classify` 输出四个信号，各管一件事：`intents`/`primary_intent` 选 Agent，`route` 选 ReAct 还是 Plan&Execute，`retrieval` 选 SQL/RAG/混合，`sub_intent=gear_plan` 进 `trip_gear`。多意图最多保留两个，只有 `shopping + travel` 走并行分支。

## 行程子系统（`app/graphs/subgraphs/trip_gear.py`）

### 链路

```text
derive → plan → gear → owned → shop → verify ─┬→ repair（自动修复）→ verify
   └→ clarify（缺目的地/天数）                ├→ verify_clarify（带代价的提问，≤2 轮）→ verify
                                              └→ finalize（不可行：替代方案 + 放弃清单）
```

两个 `clarify` 分工不同，**不要合并**：`derive` 后那个补"能不能开始规划"的槽位；`verify` 后那个补"方案到底行不行"的判断依据，只有算过一遍方案才知道该问什么。

状态字段：`facts`、`constraints`、`itinerary`、`gear`、`owned`、`products`、`issues`、`revision`、`repair_options`、`verdict`、`clarify_rounds`。

### 校验结论

| 状态 | 含义 | 出路 |
|---|---|---|
| `feasible` | 硬约束全满足 | 直接输出 |
| `repairable` | 有动作能消除全部 critical 且省得够 | 自动修复（`auto_applicable` 决定要不要先问） |
| `needs_input` | 改不动，但缺的是信息 | 发起一次有价值的提问 |
| `infeasible` | 改不动也问不出来 | 输出替代方案与放弃清单 |

判定顺序（`verdict.py`）：事实型严重问题（温标不够、无货、天数不符）直接 `infeasible`，不被无关的信息缺口冲淡；只剩预算缺口且省不回来时判 `needs_input`，把取舍交还用户。

### 代价

`cost(λ)=Σ w_i·δ_i`，`w_i = 硬度（hard 1.0 / soft 0.6）× 来源置信度（用户 1.0 / 工具 0.9 / 规则 0.8 / 推断 0.5）`。

判定用归一化幅度 `capped∈[0,1]`，排序用未归一化 `raw`——全用归一化会让"超支 2 倍"和"3 倍"同价，修复提前停住。τ 只决定"能否不经确认就动手"，**不参与可行性判定**。

### 修复动作（`repair.py`）

| 动作 | 作用对象 | 需用户确认 |
|---|---|---|
| `downgrade` 降档 | 商品 | 否 |
| `skip` 省略 | 可选装备 | 否 |
| `change_lodging` 换住宿 | 住宿 + 露营三大件 | **是** |
| `soften_activity` 砍收费项目 | 行程条目 | 否 |

`change_lodging` 只在"省下的装备钱 > 住宿差价"时提出；`apply_action` 遇到需确认动作抛 `PermissionError`，不会被自动执行。住宿费单列 `DayPlan.lodging_cost`，不摊进条目——否则算不出"换住宿贵了多少"。

### 规格匹配（`spec_match.py`）

三态 `ok` / `insufficient` / `unknown`。`unknown` 既不默认放行也不默认拦下，标 `blocking_risk` 交给追问。规格来自商品库 `products.specs`（JSON 字符串需解析）、用户自述、商品描述兜底（仅限真有规格语义的品类）。

### 预算利用（`budget.py` + `spend.py`）

预算不是上限而是计划：用户报 5000 说的是"我打算花这么多"。所以选完商品后如果有余额，
在同品类里**换更贵且更好的款**，直到钱用得差不多。

四条边界：只用库里的商品；不超预算；不降级保障（规格要求仍须满足，规格未知的候选跳过）；
不碰用户明说的条件（不换住宿、不改行程）。

实现上有两个坑：候选必须按**装备名**（"帐篷"）检索，用商品名只会命中它自己；先收集候选
再按差价从大到小落地，按装备顺序一路换会因顺序依赖把钱剩下来。差价按**数量**算
（睡袋 ×2 就是单价差的 2 倍）。钱真的花不掉时说"库里没有更贵的可用款"，不凑数。

### 商品匹配与打分选品（`shopping.py` + `select.py` + `spec_schema.py`）

三条硬规则：**搜不到就留空**（库里没有的不编）、**无货不当候选**、**近似替代必须标注**
（`保暖层→羽绒` 要写"这是替代款"）。

选品从"按价格取第一个"改成**打分**：

```
score = 0.45 × 参数匹配 + 0.25 × 参数质量 + 0.20 × 口碑 − 0.15 × 预算占用
```

- **参数匹配**：先过硬门槛（温标/容量/防水不达标直接出局），达标再看**余量**（10℃ 的夜里
  -15℃ 睡袋比卡线的 5℃ 棉睡袋分高），规格未知只给一半分；
- **参数质量**：按品类参数表归一化打分，方向由 schema 决定；
- **口碑**：按固定刻度（3.5~5.0）归一化，不用候选内 min-max——否则一堆 4.8 里 4.8 会被算成 0 分；
- **预算占用**：同样的钱能买到更好时不浪费。

**参数表按品类分开**（`spec_schema.py`）：羽绒服看蓬松度/充绒量/绒占比，手机看 CPU/内存/存储，
帐篷看季节/打包重量。没有一套通用字段能同时描述这两类商品。`capacity_persons` / `volume_l`
这类"越大越好"是错的——两人出行不需要四人帐篷，所以按**接近需求**打分，超出需求扣分。

品类约束防止跨品类误命中：`保暖层` 只在"服装"里找，`羽绒` 不会把羽绒睡袋捞进来。

**预算不够时不选**：旧实现会退到"最便宜也要买"，用超支换一个看起来完整的清单；现在留空并
标注"预算不足，未选款"，`verify` 也会把它和"库里没有"区分开。

展示上**不露规则内部的装备名**：买到的东西只写「为什么带 → 买哪件」
（`- （夜间最低 3℃） → 迪卡侬 轻羽 中厚羽绒服 ¥775`），没买到的才显示装备名。

### 记忆与偏好（`preferences.py` + `app/memory/extractor.py`）

记忆分两件事用：`owned` 判定避免重复买；`preferences` 抽品牌与住宿倾向，进入选品做
同档优先，并在商品行标"按你偏好的品牌"。

候选记忆按**耐用性**分级：一次性行程事实（目的地/天数/预算）标 `transient`，默认不提示
写入长期记忆；稳定偏好（喜欢、一般、忌口、常住）标 `durable` 才进确认。正则兜底抽出来的
必然是一次性事实，默认全部过滤——否则每聊一次行程就弹三条"要不要记住目的地"。

### 采购可履约（`purchase.py`）

四态 `ok` / `late` / `out_of_stock` / `unknown`。无货优先于时效；已出发判 `unknown` 不外推负数；赶不上时按"到货天数 + 库存"筛替代款，价格只排序，另给"到店自提"退路（不建模地理位置，标假设）。

## 商品目录

`data/products.db` 是**自建目录**，不是外部电商数据；它不进版本库，按 README「初始化示例数据库」
的四条命令重建（init_db → migrate_product_specs → add_outdoor_products → generate_catalog）。

| 来源 | 数量 | 说明 |
|---|---|---|
| `seed` | 36 | 早期手工写的种子商品（真实品牌与价格，作为参照） |
| `generated` | 2000 | `scripts/generate_catalog.py` 按矩阵生成，带规格与评价 |

生成是**确定性的、零 token**：名称按电商真实写法「品牌 + 系列 + 品类叫法 + 颜色/版本」
（`牧高笛 云尚 双人帐篷 卡其`），价格随档位，规格随品类，
评价由「方面 × 情感 × 证据」拼成，并且**绑定商品真实规格**（防水 20000mm、温标 -5℃）。
评价不是随机句子：同一商品的 5 条不撞方面，正面/中评/差评比例由档位质量决定，负面评价
只出现在属性确实弱的商品上——否则检索会答出自相矛盾的结果。

评价去重靠「情境前缀 + 正文 + 收尾」三层拼装，实测一万条里唯一文本约 74%（纯句子池
会掉到 2%，一万条评价只有两百种说法）。重跑安全：按名称判重，改库前自动备份；
`--replace-generated` 会先删掉上一批生成数据再灌入（同样先备份）。

**商品名不能直接用规则里的装备名**（"保暖层""防水鞋"是内部说法）。真实叫法靠
`_descriptor()` 映射，并且要迁就检索别名：`保暖层→羽绒` 要求档位名里必须含"羽绒"，
所以保暖层只有羽绒服档位，没有"抓绒衣"；`儿童衣物→儿童` 要求名字里带"儿童"。

## 事件契约

custom chunk 与旧 SSE 一致，UI 无需改动：

```json
{"event": "token", "data": {"token": "你"}, "step": "generate", "timestamp": 0.0, "trace_id": "..."}
```

子图事件靠 `subgraphs=True` 透传，命名空间在 `_split_chunk` 里剥掉。结构化结果单发 `plan_result` 事件（`facts` / `constraints` / `itinerary` / `gear` / `products` / `issues` / `verdict` / `revision`），前端据此渲染面板，不必解析 Markdown。

## 评测

场景集 `data/evaluation/trip_gear.spec.jsonl`，一行一个场景；`expect` 是**人工标注**，不是被测代码算出来的。`products` 内联，评测不依赖数据库当时状态。

```bash
python scripts/eval_trip_gear_specs.py     # 输出 data/evaluation/trip_gear.spec.report.json
```

关键指标：`new_accuracy` / `legacy_accuracy`（对照"只查理由里有没有'温标'"的旧逻辑）、`legacy_reported_insufficient`（旧逻辑恒为 0）、`legacy_false_accept`、`spec_unknown_rate`。评测确定性：不调模型、不联网，同一份场景集结果一致。

## 新增一个场景子图

1. 定义状态：通用字段放 `app/graphs/state.py`，该场景专有的就近写在子图模块里。
2. 写节点，签名 `(state, runtime: Runtime[TurnContext])`，事件用 `runtime.context.sink_for(runtime)`。
3. `build_xxx_graph()` 里用 `retry_policy` / `timeout` 声明策略。
4. 在 `orchestrator.py` 的 `ROUTE_TARGETS` 挂节点与路由名。
5. 在 `nodes.py` 的 `route()` 给条件。

依赖注入：生产从 `services` 取能力；测试用 `TurnContext.extra` 覆盖 `planner` / `product_search` / `weather_lookup` / `memory_loader` / `max_revisions` 等。

## 文件速查

| 文件 | 职责 |
|---|---|
| `trip_gear/constraints.py` | 约束派生（规则优先、模型补充） |
| `trip_gear/itinerary.py` | 逐日行程、时间校验、无模型兜底、住宿估价 |
| `trip_gear/gear.py` | 装备需求推导规则 |
| `trip_gear/owned.py` | 已有装备判定 + 规格抽取 |
| `trip_gear/spec_match.py` | 规格匹配三态 |
| `trip_gear/shopping.py` | 缺口 → 商品检索 |
| `trip_gear/purchase.py` | 采购可履约 + 替代链 |
| `trip_gear/verify.py` | 确定性校验（约束/齐全性/规格/可履约） |
| `trip_gear/verdict.py` | 四态结论 + 修复方案 + 信息缺口 |
| `trip_gear/repair.py` | 修复动作与落地 |
| `trip_gear/tradeoff.py` | 统一代价 |
| `trip_gear/spec_eval.py` | 规格评测与改前/改后对照 |
| `trip_gear/render.py` | Markdown 展示 |

`docs/generated/` 放生成物，**不要手改**：`pipeline-spec.html/json` 由 `smartlife-agent/scripts/build_pipeline_spec.py` 生成；`current-agent-flow.html` 是早期的分支流程快照，只作查证。
