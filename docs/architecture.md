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
        ┌─────────────┐ ┌────────┐ ┌───────────┐ ┌──────────────────┐
        │ shopping    │ │ travel │ │ trip_gear │ │ customer_service │
        │             │ │        │ │           │ │ / general        │
        └──────┬──────┘ └───┬────┘ └─────┬─────┘ └────────┬─────────┘
               └────────────┴──────────────┴─────────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │ finalize →（可选）extract_memory│
                    └───────────────────────────────┘

能力面（节点内调用，不持有编排）：Tool Registry、Hybrid Retrieval（NL2SQL+RAG+Rank）、Memory（短/长/MD/审批）
```

五个节点 = 五个 Agent = 五条工具白名单，名字一一对应（`agent_capabilities.AGENT_TOOL_POLICIES`）。
`travel` 与 `trip_gear` 共用同一张行程图，差别只有 `with_gear`：前者剥掉装备段（`gear` / `owned` / `shop`），见下文「行程子系统」。

分层：`app/main.py`（UI，只调 TurnGraph）→ `app/graphs/orchestrator.py`（`build_turn_graph()`）→ `app/graphs/subgraphs/*`（节点签名统一 `(state, runtime)`）→ `app/services/*`（无编排的领域能力）→ `app/retrieval` / `app/memory` / `app/tools`。

## 四个"原生"约束

| 维度 | 原生做法 | 已废弃做法 |
| --- | --- | --- |
| 依赖注入 | `context_schema=TurnContext` + `runtime.context` | 编排器实例属性 + `config["configurable"]` |
| 流式 | `runtime.stream_writer(...)` + `astream(stream_mode="custom", subgraphs=True)` | 自研 `EventQueue` |
| 重试/超时 | `add_node(..., retry_policy=..., timeout=...)` | 自研 `run_policy_node` |
| 节点计时 | `stream_mode="tasks"` 的 `node_started` / `node_succeeded` | 包装器里手写日志 |
| 人机中断 | `interrupt()` + `Command(resume=...)` + checkpointer | 直接调审批对象方法 |

## 顶层路由

`classify` 输出四个信号，各管一件事：`intents`/`primary_intent` 选 Agent，`route` 选 ReAct 还是 Plan&Execute，`retrieval` 选 SQL/RAG/混合，`sub_intent` 决定走 `travel` 还是 `trip_gear`。多意图最多保留两个。

判定顺序（`nodes.route`）：

1. `gear_plan`，或原话里出现露营/装备/清单/带什么/徒步/穿越/登山 → `trip_gear`；
2. `shopping + travel` 同时就绪：`route=plan_and_execute` 或带装备场景 → `trip_gear`；否则按主意图走（"买跑鞋，顺便问下三亚天气"不该被当成一次行程）；
3. `travel + plan_trip + plan_and_execute` → `travel`（同一张图，无装备段）；
4. 其余按主意图：`shopping` / `customer_service` / `travel` / `general`。

`travel` 只有一条路：`build_travel_graph()` 只挂一个 `travel_plan` 节点，跑的就是 `build_trip_gear_graph(with_gear=False)`，与 `trip_gear` 共用 `derive → plan → verify → repair → finalize` 和 01–05 面板，差别只有 `with_gear` 一个参数。**没有 `travel_chat`**：早先那条"不进规划、直接把问题丢给大模型写正文"的分支已删除——它让同一句话有时出面板、有时出整篇 Markdown，且缺天数/人数/日期时模型会替用户定。所有 travel 请求现在都进图，缺信息由 `clarify` 追问。

判定顺序与阈值（`app/nlu/signals.py`）：

| 层 | 规则 |
|---|---|
| 关键词打分 | 品类/品牌 1.0、购物动作 0.6、泛化词 0.4；地名 0.8、强旅游词 1.0、场景词 0.6、时长 1.0、地名+出行语境 0.4。跨度去重（"跑步鞋"只算一次），否定词（"不要帐篷"）直接扣掉 |
| 购物就绪 | 必须命中**实义品类/品牌**且总分 ≥1.2；只有"价格/多少钱"不算 |
| 旅游就绪 | 总分 ≥1.2，**且不能只靠地名**：地名之外没有强词/场景词/时长时，必须再配上出行语境（去/到/周末）才算。"上海和汕头哪个好玩"这类只有地名的句子交小模型 |
| 复合 | 两类都就绪 → 取分数最高的两个，`confidence=0.9` |
| 延续 | 关键词层返回 `None` 时**默认沿用上一轮意图**，但有三条例外：跨领域新任务（新品类、订单/退款、换到别的领域）切换；寒暄（谢谢/好的）不沿用也不重算；句子里没有任何本领域实词、也没有指代（"今天天气怎么样""推荐一部电影"）交小模型判。同一个领域里的换目的地/改日期仍算延续，由 derive 按槽位覆盖 |
| 低置信 | 关键词层返回 `None`（且无可沿用的上一轮）→ 小模型（<0.55 分或 JSON 坏）→ 主模型仲裁一次 |
| 模型失败 | 有关键词证据就按关键词兜底（`confidence=0.4`），没有才回 `general` |

### 关键词层只出证据，不定结论

强弱口径集中在 `signals.IntentScores`（`shopping_strong` / `travel_strong` / `service_strong`）：
品类+购买动作、强旅游词或"地名+时长/出发结构"、客服词凑够两个，才算强证据，可以直接短路；
其余"就绪但不够硬"的一律交小模型，并把关键词结论作为线索写进 prompt（`{keyword_hint}`）。
分类事件多带一个 `strength` 字段，"这次为什么没走模型"在界面上直接看得到。

理由是可纠错：以前只要有一类就绪就 `return`，错判既没有模型复核，
日志里也全是 `method=keyword`；而词表是开放集，靠补词永远追不上。
出口兜底不变——模型失败时仍按关键词证据回落（`confidence=0.4`）。

`route` 现在**没有图内消费方**：删掉 `travel_chat` 之后，`general` 恒为 ReAct、`travel` 恒为规划图，没有任何节点按 `route` 分叉。它只剩展示与日志价值（分类卡片里的"策略"）。`shopping`、`trip_gear`、`customer_service` 本来就不看它。要恢复按复杂度分叉时，应在子图内部决定，不要再回到"分类层替子图做决定"。

`sub_intent=gear_plan` 的劫持改成显式枚举 + 中文限定词（`nodes._is_gear_plan`），不再用 `"gear" in sub_intent` 的包含判断。

`prepare` 每轮都会把**本轮用户消息**接在检查点带回的历史后面，并把历史里
"带 `tool_calls` 却没有 `reasoning_content`"的 assistant 消息连同它的 tool 结果一起丢掉——
thinking 模型会拒收这种消息（400），而检查点会让它每轮重现。

`general`（原 `react` 节点）绑到工具时会注入一条系统消息（`TOOL_USE_RULES`）：只查确实不知道的实时事实、查不到就说查不到、不拿工具结果拼行程；没有工具时不注入。

## NLU 层（`app/nlu`）

一句话只解析一次，三处共用，这是"同一句话在两条链路抽出两个值"的根治点。

| 文件 | 职责 |
|---|---|
| `numerals.py` | 中文数字按位值组合还原（三千五=3500、一万五=15000、十二万=120000），金额连单位切分 |
| `lexicon.py` | **唯一**一份词表：品类、品牌、旅游词、场景词、地名、客服词、检索需求词、否定词 |
| `slots.py` | 出发地、目的地、天数、人数、总预算、每晚住宿预算、日期、住宿、交通、同行儿童 |
| `signals.py` | 意图加权打分 + route 判定 + 兼容旧调用点的谓词 |

三条硬约定：

1. **`SharedConstraints` 与 `TripFacts` 都从 `slots.extract_slots` 取值**，不再各写正则。
2. **`people=None` 表示"用户没说"**，计算按 `effective_people`（=1）处理，展示写"未指定（按 1 人计）"，不能伪装成用户说了 1 人。
3. **预算先剔除装备预算和商品单价**，再按"总预算 > 上限 > 下限 > 唯一金额"取；"装备预算1000，总预算3000"取 3000，"预算3000，手机5000元"取 3000。

日期解析需要"今天"时由调用方注入（`extract_slots(text, today=...)`），保证评测可复现。

入口有一份**人工标注的语料回归**：`data/evaluation/nlu_corpus.jsonl` + `tests/test_nlu_corpus.py`。每行一句话加期望槽位，覆盖否定、子景点、A 到 B、人均/区间预算、农历节日、月份、当天来回等说法。改解析前先看这份语料；遇到没覆盖的说法，加一行，而不是改一条逻辑。

### 出发地和目的地分两个槽位

同一句"我在广东，我要去上海玩三天"里有两个地名，方向必须分清：

- `parse_origin` 抽"我在 X / 我们住在 X / 从 X 出发"；`parse_destination` 先扫强动词（去/到/回/飞/前往），命中直接返回。
- **"在"是弱动词，排在强动词之后**。"我/我们/本人/咱/俺"（以及"住"）后面紧跟的"在"是"人在哪儿"，不当目的地——否则"我在广东"会先于"去上海"胜出，整趟行程方向反了。
- 地名里不会出现"我 / 的 / 了 / 在 / 找 / 去"，候选里带这几个字的是半截句子，`_valid_destination` 直接拒。
- 两个槽位各自进约束清单（`出发地：广东` / `目的地：上海`），出发地也进规划提示词；`_merge_facts` 会合并它，追问轮里不会丢。
- "在 X 出发去 Y"同样算出发地；**省份不是城市**（`lexicon.PROVINCES`）：广东/浙江这类拿不到天气和路线，`derive` 会反问具体城市，不拿省名去猜一个坐标。
- 地名表兜底（没有动词可用时）取**文字里最先出现**的那个地名。以前按"名字长优先"迭代、命中即返回，命中顺序取决于词表顺序：追问卡多选拼出的 `目的地：成都、杭州` 会返回杭州，第一站变成用户没排在第一个的城市。多城顺序就是行程顺序，只能按出现顺序来。
- "酒店一晚不超 400"是**每晚住宿预算**（`lodging_budget`），和整趟预算分开记；"一晚"也不当时长算，否则一周行程会被读成两天。

### 槽位补全（可选，默认关闭）

规则抓不到的写法（"去石塘待几天"）由 `app/services/slot_assist.py` 兜一次：
`derive` 与 `extract_requirements` 在**缺必填槽位时**才调用，超时 4.5s。

- 开关：`SMARTLIFE_SLOT_ASSIST=1`。默认关，因为"猜错目的地"比"多问一句"代价大。
- 只填空缺，不覆盖规则结果；结果来源标 `模型补全`，约束清单里看得见。
- 防幻觉：目的地必须能在原话里找到；天数 1~60、人数 1~100、预算 >0 才收，
  过期的出发日期直接丢。

## 行程子系统（`app/graphs/subgraphs/trip_gear.py`）

### 链路

`build_trip_gear_graph(with_gear=True/False)` 两种组装：`trip_gear` 是全量，`travel` 跳过 `gear` / `owned` / `shop` 三段。`verify` 收到 `with_gear=False` 时不判装备类约束（标 `unverified`），`render` 不输出装备分区。

```text
derive → budget_split → plan → gear → owned → shop → verify ─┬→ repair（自动修复）→ verify
   └→ clarify（缺目的地/天数/人数/日期）                      ├→ repair_confirm（要用户点头的改法，≤1 轮）→ verify
                                                              ├→ verify_clarify（带代价的提问，≤2 轮）→ verify
                                                              └→ finalize（不可行：替代方案 + 放弃清单）
```

`budget_split` 是**规划之前**的一步：有预算的 trip_gear 会停下来问一次"行程和装备各拿多少"，用一个 0–100、步进 10 的**滑动条**（行程占比）。两端不是"比例很小"而是"不要另一边"：`100` 表示不要装备，`state["with_gear"]` 置假、装备段整段跳过，输出等价 travel；`0` 表示不要行程，不进 plan，只做装备推导与选品，面板不出逐日行程。中间值写进 `facts.trip_cap` / `facts.gear_cap` 与约束清单，plan 按行程额度排、shop 按装备额度买。不再用"总预算 − 行程实际花费 = 装备预算"这条旧口径——它把行程省下的钱自动变成更贵的装备，用户没有选择权。用户已经明说"装备预算 1000"时不问；`travel`（没有装备段）整笔预算就是行程预算，也不问。

滑块**只回一个整数**（行程占比），所以界面上必须同时给出另一端：问题 payload 带 `budget`，`app/main.py` 用 `split.shares()` 在滑块两侧实时显示“行程 90% · ¥4500”与“商品（装备）10% · ¥500”。两端不再露出原生刻度/拇指数字（CSS 隐藏 `stSliderTickBar` / `stSliderThumbValue`），否则左边只剩一个 `0`，用户看不出这是哪一端；两侧标签带名字，跟着滑块变化。问题还带 `estimate`（机票+住宿估算），行程额度低于它时滑块下方直接提示“这两块已经超过行程额度，还没算地面花费”，避免用户选完才发现必然超支。

补答不完整时（比如只答了人数、日期那句没解析出来）不会直接收尾：`after_clarify` 把还缺的项
再问一轮，**只问缺的那几项**，而且**不设轮数上限**——用户在答就一直问，直到补齐。
两个出口按"用户有没有在答"区分：答了但没解析出来 → `ask_continue=True` 继续问；
根本空回答（关掉卡片）→ 收尾，不拿同一张卡反复拦人。
以前这里直接 `end`，界面上只剩一句"还需要补充：出发日期"，没有卡片可点，这一轮就卡死了。

两个 `clarify` 分工不同，**不要合并**：`derive` 后那个补"能不能开始规划"的槽位；`verify` 后那个补"方案到底行不行"的判断依据，只有算过一遍方案才知道该问什么。

`repair_confirm` 不是 `repair` 的重复：`repair` 只能动装备（降档/省略），而 `change_lodging`（露营改住民宿）会推翻用户明说的条件，`apply_action` 遇到 `requires_confirm` 默认抛 `PermissionError`。`verify` 把这类动作放进 `confirm_options`，节点用 `interrupt()` 问一次，用户回「调整」才 `force=True` 执行，回「不用」就原样进 `finalize`；`confirm_rounds` 上限 1，问第二遍是纠缠。自动修复永远排在它前面——能自己做完的事不该先问人。

### 缺就问，不默认：目的地、天数、人数、日期、出发地、交通、住宿

"能不能开始规划"的条件——目的地、天数、人数、出发日期、**出发地、交通方式、住宿**——
缺一样就停，用追问卡片问，不拿默认值替用户决定：

- **出发地**：没有它就估不出大交通（机票/高铁段数），也说不清第一天从哪出发。
  以前缺出发地就"城际交通未估算"，面板上那个"预算还剩多少"是假的。
- **交通方式**：机票、高铁、自驾的估算差一个量级；不问就只能留空。
  票价仍是**披露的估算单价**（机票 ¥800/段、高铁 ¥120/段），不是报价。
- **住宿只问每晚预算，不问类型**：默认就是酒店、两人一间。以前没说就按
  "经济型 300/晚"算——那是用户没同意过的假设，会直接改变"预算够不够"的结论。
  现在问一次，选项值仍写成一句能解析的话（`住宿：酒店，每晚400元`），
  自己填「450」也认（`_LODGING_BUDGET_LABEL`）。民宿/露营不进这一题：
  **露营只在原话里出现"露营"时才生效**（`parse_lodging` 里露营词先于酒店词）。
  没答出来就**不估算**，`estimate_notes` 里写"住宿未计入总预算"，让缺口可见，
  不去编一个数。
- 露营没有"每晚房价"这一说：`_merge_facts` 会把 `lodging_budget` 清掉，
  也丢掉答案里那半句酒店报价，避免清单里同时出现"住宿：露营"和"每晚 ≤400"。
- **票价与住宿优先用模型自己估的**：规划提示词要求模型把大交通写成当天条目
  （"高铁 上海→杭州（2 人）¥480"），住宿不写成条目。`apply_estimates(trust_items=True)`
  只补空的：模型写了交通条目就不再叠一层总额估算（否则同一笔钱算两遍），
  模型给了 `lodging_cost` 就按它的。兜底行程（模型没回来）仍按披露单价估。

- 追问补答整轮没被读懂时（`remaining` 与上一轮的 `gaps` 完全相同），正文要明说
  "刚才那条回答没读出：住宿"，而不是把同一张卡默默再摆一遍——否则用户会以为点了没生效。
- `_blocking_gaps(facts)` 是唯一口径，`derive` 和 `clarify` 都调它，别各写一份。
  顺序即卡片的阅读顺序：出发地 → 目的地/具体城市 → 天数 → 人数 → 出发日期 → 交通 → 住宿。
- 用户说了日期 → 约束清单写 `出发日期：2026-11-14`，来源"用户原话"。
- **不能只写进 `facts` 不写进清单**。早先就是这样，于是用户明明说了日期，清单里也看不见；而校验器又拿不到日期，逐件刷"没有出发日期"。
- **没有日期时不判采购时效**：拿"估计的今天"逐件算到货，会把每件待购都判成赶不上，结论直接 `infeasible`，还会把真正的冲突挤出 05 面板。日期落地（或用户改期）后再判。
- 用户点"自己填"或干脆不答时，`ensure_start_date` / `ensure_people` 仍会写"未指定（按今天计 / 按 1 人计）"，来源"默认规则"、软约束；面板末尾那句"默认值：…"只在这种情况下出现。
- `ensure_start_date` 是**重写**不是追加：追问里补的日期要替换掉占位那条，否则同一个 key 会并存两条。`_merge_facts` 也必须合并 `start_date`，不然用户补答的日期会被静默丢掉。

01–05 面板只做展示：约束的 `hard`/`status` 仍参与判定（verify 判 critical、tradeoff 算权重），只是不再各占一列。

状态字段：`facts`、`constraints`、`itinerary`、`gear`、`owned`、`products`、`issues`、`revision`、`repair_options`、`confirm_options`、`confirm_rounds`、`verdict`、`clarify_rounds`。

### 多城市：城市落到天，不是落到"目的地"

用户说"广州、深圳、珠海各玩两天"时，行程不是"广东 6 天"，而是每天有城市：

- `slots.parse_cities` 按出现顺序抽城市（排除出发地）；**省份不算城市**，`广东` + 多个城市时不再反问，只有省份没有城市才问。
- 出发地有两种写法：`从上海出发` 和裸的 `上海出发`。后者没有介词，只认地名表里的词——否则`后天出发`会被当成出发地。起点一旦漏抽，它就会以「行程城市」的身份混进来（`上海 → 汕头`），前半程排成出发地景点。
- 省份 + 「很多城市/你定」：用户点名的城市**保留**，缺的用 `PROVINCE_CITIES` 补，按天数封顶（7 天 3 城）；以前是整体替换，一句「要有汕头」会被默认城市挤掉。
- "各玩 N 天"是每城天数，整趟 = N × 城市数（来源标"规则推导"）。
- `itinerary.assign_cities` 按顺序均分、余数给前面（7 天 3 城 = 3/2/2），写进 `DayPlan.city`；**模型写了别的城市也不采信**（`_from_plan_output` 用分配表覆盖），否则面板标题写广东、行程却在上海。
- 天气按"城市 × 天"取：每个城市查整段窗口，再按分法挑当天的行，清单写成 `天气（第3天 11-11 深圳）`。
- 渲染标题带城市（`### 第 3 天（深圳）`）；兜底行程的条目文字也换成当天城市，不再全是首城。

### 用户指定的固定活动

活动证据只有**场所**（剧院、博物馆、公园），拿不到"某天演什么"——Ticketmaster 要 Key 且无中国业务，
秀动/大麦要签名或反爬，场馆官网是前端渲染。所以场次级信息不查，改由用户提供。

两条输入，走两条不同的路：

| 输入 | 落到哪 | 效果 |
|---|---|---|
| "已订好第 2 天 19:30 的陶艺工作坊，200 元" | `TripFacts.fixed_events`（`status="booked"`） | 钉进那天，其它安排让路，票价进预算 |
| "有一天去找一个我在浦东的朋友" | `TripFacts.fixed_events`（`status="appointment"`，地点=浦东） | 占半天排进某一天；没说哪天就排中间那天 |
| "想参与一点活动" | `TripFacts.wants_activity=True`（软约束） | 只影响行程取向，**不产生任何日程** |

`apply_fixed_events` 的三条硬规则：

- **只钉 `booked` 和 `appointment`**。`planned` 只是"想去"，排不下就算了，不能挤掉别的安排。
- **`booked` 没说是第几天就不钉**（`day=0`）。猜一天等于替用户决定哪天去；`appointment` 相反——它是"这趟得留出半天"，没说哪天就排中间那天（第一天赶路、最后一天返程）。
- **让路的是可调整条目**。当天超时就从末尾删非固定条目；那天原本排了返程而 `booked` 活动在它之后，删的是返程——已订活动是用户已经买票的外部约束，删它等于替用户退票。`appointment` 不删返程，只清掉和它撞时段的安排（否则同一天 14:00 会排两件事）。

抽取上的坑：`slots.parse_fixed_events` 必须两道闸同时成立才认——已订语气（已订/买票/预约/报名）
**且**名字里有活动类型词（音乐会/工作坊/展览……）。只留一道闸会出事："想去上海玩两天"会被
抽成一个叫"上海玩两天"的活动，"想参加一点活动"会被抽成一个叫"一点活动"的日程。
语气词还要按长度降序匹配，"已订好陶艺工作坊"不能被"已订"抢走而剩下"好陶艺工作坊"。

约见走另一个抽取器 `parse_visits`：约见动词（找/见/看/探望/拜访）+ 人（朋友/同学/亲戚…）同时成立才认，
地点从"在 X 的"里取。它**不需要**活动类型词，因为"找朋友"本身就是事由；也不需要已订语气，
因为用户说"有一天去找朋友"是在提要求，不是在描述已买票。两个抽取器的结果合并进 `Slots.fixed_events`，
靠 `status` 区分硬度。

### 追问卡片：一次问多个，选项可点，该多选的题能多选

`interrupt` 的 payload 里除了 `missing` / `question`，还带 `questions`：

```
{"key": "days", "title": "玩几天？", "answer_prefix": "天数",
 "options": [{"label": "3 天", "value": "3 天"}, ...], "allow_custom": true,
 "multi": false, "hint": ""}
```

- 生成器是 `app/services/clarify_questions.py`：缺失项在前，人数/预算/出发日期这类"顺手能量化"的在后；校验后追问（温标、预算口径）也有预置选项。
- 前端把每题渲染成药丸选项 + 一个"D · 自己填"，多题一次提交；点完拼成 `目的地：杭州；天数：3 天；人数：2 人` 回给图，不用再过一遍自由文本理解。
- 追问补上的目的地/人数要**重写**清单里那条旧值（`ensure_destination` / `ensure_people`），否则事实改了、清单还写着"未指定"。

**每道题的选项值都是"能直接被槽位解析的一句话"**，所以点完不用再过一遍自然语言理解：

| 题 | 选项 | 拼回来的话 | 解析成 |
|---|---|---|---|
| 出发地 | 上海/北京/广州/深圳/成都/杭州 + 自己填 | `出发地：上海` | `origin=上海` |
| 交通 | 坐高铁 / 坐飞机 / 自驾 | `交通：坐高铁` | `transport=train` |
| 住宿 | 经济型 ¥300 / 舒适型 ¥500 / 高档 ¥800（按间·晚，默认酒店） | `住宿：酒店，每晚400元`；自己填「450」也行 | `lodging=hotel`、`lodging_budget=400` |

**点的是标签，回给图的是值**：两者不一定相同（「经济型 ¥300/晚」→「酒店，每晚300元」），
所以界面必须过一遍 `clarify_questions.option_values(question)` 再拼句子。
直接把标签拼回去，槽位解析不出来，同一张卡会一轮一轮问同一件事——这条踩过，
`tests/test_clarify_questions.py` 里每道题的每个选项都走"标签 → 值 → 拼句 → 重新派生"
验一遍。自己填也认口语："住宿：300" 直接读成"酒店、每晚 300"。

**`multi` 是逐题的，不是全局开关**：只有答案是集合的题才多选。

- 目前只有一个：`cities`（"广东去哪几个城市？"）。一天一个城市，串几个城市是行程的形状；
  单选只能拿到一个城市，"广东玩一周"会退化成单城。
- 天数、人数、预算、出发日期、预算比例一律单选：这些题多选本身就是自相矛盾的答案，
  界面连多选控件都不给（`st.radio` 与 `st.pills` 是互斥分支，不会两个都出现）。
- 多选的答案在进图之前塌成一个值（`广州、深圳、珠海`），由 `clarify_questions.value_text`
  拼、`slots.parse_cities` 解——下游（`derive_constraints` / 约束清单 / `assign_cities`）不需要
  知道它来自多选。多选的"自己填"输入框是叠加不是替换：选了广州再填佛山，两个都在。

### 校验结论

| 状态 | 含义 | 出路 |
|---|---|---|
| `feasible` | 硬约束全满足 | 直接输出 |
| `repairable` | 有动作能消除全部 critical 且省得够 | 自动修复（`auto_applicable` 决定要不要先问） |
| `needs_input` | 改不动，但缺的是信息 | 发起一次有价值的提问 |
| `infeasible` | 改不动也问不出来 | 输出替代方案与放弃清单 |

判定顺序（`verdict.py`）：事实型严重问题（温标不够、无货、天数不符）直接 `infeasible`，不被无关的信息缺口冲淡；只剩预算缺口且省不回来时判 `needs_input`，把取舍交还用户。

`verify` 还单独核对**行程分配额度**：总预算没超、但行程这一端超过 `trip_cap` 时记一条 minor “行程超出分配额度”，04 面板也写“分配 90% / 已用 150%”。结论不能只埋在 05 里：`plan_panels_html` 顶部按 `verdict.state` 放“当前条件不可行 / 需要你确认 / 可以按代价调整”的横幅，`render` 正文第一行也写同一句。

`verify_clarify` 里用户改了预算时，不能只改 `facts.budget`：旧分成和按旧额度排的行程都失效。节点把 `trip_cap` / `gear_cap` 清空、删掉旧的 `budget_split` 约束，`after_verify_clarify` 回到 `budget_split` 重新问比例并重排；只改数字不重排，面板会写“预算 6000”、行程还是按 4500 排的。

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

### 到店自提与“赶不上”

采购判定原来只有“线上到货”一条路：明天出发、到货 3 天，即使行程第 2 天就安排了“商场逛街”，也判 `到货赶不上` 并进 `infeasible`。现在 `purchase.shopping_days(itinerary)` 识别行程里的商场/到店/购物活动，线上赶不上但有自提窗口时判 `STATUS_SELF_PICKUP`，`verify` 生成一条 **minor** 提示“可到店自提（假设同城/有货）”，不再当 critical。05 用一条“可到店自提（不判不可行）”的提示交代；Markdown 单独有“## 到店自提”一节。

边界不变：没货还是 `out_of_stock`；没有自提窗口还是 `late`；自提不建模门店位置，文案必须写“假设同城/有货”；如果商品在自提日之前就要用，提示里要写“更早要用需提前买”。

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

### 机票/住宿估算（`estimates.py`）

`total_cost` 以前只加"条目 + 住宿"，住宿在用户没指定时还是 0；机票/高铁没有字段，模型只能把"机票单独计入城际交通"写进文字。于是 5000 元的方案只算了地面花费，"剩余 1176"是假的。

现在 `estimates.py` 用一组**会展示给用户的固定单价**补两块：机票 ¥800/人/段（往返 2 段）、城际高铁 ¥120/人/段、住宿 ¥300/间/晚（两人一间，露营 ¥50/人/晚）。`apply_estimates` 在 `plan` 之后写进 `Itinerary.intercity_cost` 与每天的 `lodging_cost`，住宿只算 `days - 1` 晚（最后一天返程不收费）。`total_cost` 把城际加进去，所以 verify、04 面板、预算快照用的是同一口径；`Itinerary.estimate_notes` 原样贴到面板和 Markdown，标明"估算"。

04 面板把“分配比例”和“使用比例”分开：`行程 ¥6735（额度 ¥4500 · 分配 90% · 已用 150%）`。超支时文字不能再写“已花 100%”——进度条可以截在 100%，但 `已用` 必须显示真实的 144%；否则用户看到“超支 2188”和“已花 100%”会以为没超。

规划提示词额外给"城际估算 / 住宿估算 / 地面可花上限"三行，模型按**地面可花上限**排每日花费，而不是拿整笔行程额度去排——机票和住宿已经先扣掉了。没给出发地/目的地、或交通方式没指定时不猜，返回 0 并写"未估算"。

### 商品匹配与打分选品（`shopping.py` + `select.py` + `spec_schema.py`）

三条硬规则：**搜不到就不进清单**（库里没有的不列）、**无货不当候选**、
**同类替代要标注**（"羽绒服"搜到"轻薄羽绒外套"要写"这是按羽绒服检索到的同类款"）。

### 选品：打分兜底 + 模型挑款

用户点名"一套衣服"时，模型挑款仍看全部女装候选（连衣裙/半裙/上衣），但打分兜底在预算够连衣裙时会收敛到连衣裙——否则"一套"会退成半裙。`GearShopper._pick_candidates` 只影响兜底，不影响模型看到的候选。

候选由检索+打分裁剪，最后的"选哪一件"可以交给模型（`app/trip_gear/select_by_model.py`），
也可以退回打分（`select.py`）。默认开，`SMARTLIFE_MODEL_SELECT=0` 关闭。

成本控制是设计的一部分，不是事后优化：

- **一次调用挑完**，不是每件装备一次：十来项装备只问一遍；
- **模型只做四选一**：候选已由 `select.rank` 裁到每项最多 4 个，模型不参与检索；
- **没有选择就不问**：候选少于 2 个的项直接走打分；
- **只给 id / 名称 / 价格 / 至多三个参数**，不给完整描述；
- **结果缓存一小时**（同一组候选 id + 预算），重复行程不再付费；
- **任何异常都回落**：超时、选错 id、超预算、商品被别项占用 → 打分结果照常出。

量级：每次出行 1 次调用、约 1k token；逐件调用是 10 次调用、6k+ token，
差一个数量级，且**不随商品库变大而增长**（候选数与商品数无关）。

### 已有装备与清单口径

清单回答的是"这次推荐你买什么"，不是"你还缺什么"；所以口径只有两条：

- **只列商品库里真有的**：派生出来的需求如果检索不到商品（或全部无货），
  在 `shop` 阶段就直接从清单里移除——不占行、不报数、不留"未匹配"字样。
  想知道"缺了什么"去看 `verify` 的问题列表，那是校验的事，不是清单的事。
- **用户说了就记**：`apply_owned` 只做词形包含匹配（"西装外套"含"西装"），
  **不维护"西装外套→正装"这类别名表**——商品一多就维护不动，而且它只解决
  被枚举到的那几个词。匹配不上的已有物品也照样进"已有"列，用户说了却不显示
  等于没听见。

清单里也不再有"自备"分区：身份证/手机这类底座既不是商品、也不是用户声明的
已有物品，老版本把它们写死在 `BASE_ITEMS` 里每次必加，现在整块删掉。

### 用户点名的购买需求：跨追问保留，不静默丢

"女生想买一套衣服带去海边"不是从"海边"反推出来的装备，而是用户点名要买的。`slots` 之外由 `constraints.parse_purchase_items` 抽成 `TripFacts.purchase_items`，并写一条 `点名购买：…` 约束；`derive` 在追问轮从 `history_text` 把上一轮的购买项合并回来（`_merge_facts` 也要合并），否则 `state["user_message"]` 只剩"预算分配：90"，gear 永远看不到。

`gear.derive_gear` 把购买项映射成商品库类目 `女装`，打 `user_requested=True`；检索时"女装"命中连衣裙/半裙/上衣，由选品模型按场景挑（`select_by_model` 的系统提示要求"一套衣服"优先连衣裙）。`GearShopper.fill` 的预算省略循环**不砍 `user_requested`**：额度不够时先砍可选装备；真装不下就留在清单里标"预算不足，用户点名需保留"，由 verify 披露，而不是从末尾静默删掉。

点名的“一套衣服”如果只选到半裙/上衣、连衣裙被额度省略，`verify` 会补一条 minor “装备：女装 未选到连衣裙”，03 脚注写“想保留连衣裙/泳衣：把装备占比调高（如 20%），或去掉雨伞/防晒”。不替用户硬选，但必须把取舍说出来。

### 选品的人群过滤

`T恤` 这类通用词会同时命中童装；按价格打分时儿童T恤更便宜，成人行程会推儿童款。`shopping._candidates` 在 `facts.with_kids == False` 时过滤 `category/subcategory` 含"童"或名字含"儿童/童装"的候选；`with_kids` 为真才放开。

派生用的词也换成了**真实商品词**（羽绒服 / T恤 / 登山鞋 / 雨伞 / 儿童 / 西装），
不再用"保暖层""换洗衣物""防水鞋"这类内部叫法——内部叫法一旦存在，就必然要
一张别名表去对商品，这正是维护不动的根源。

模型推断出来的"记得带用户已有的 X"在 owned 判定之后丢弃
（`constraints.drop_owned_duplicates`）：东西已经标成"已有"了，
再挂一条"模型推断 / 软 / 未校验"只会让用户以为系统没认出来。

## 全球地名与活动证据

用户会随口问世界任何城市，所以「地名 → 坐标」和「当地有什么」必须是真数据源，
而且**拿不到时要说拿不到**。这一层由 `app/tools/geo.py` + `app/tools/providers.py` 承担。

### 地名解析（`geo.resolve_location`）

只靠 Open-Meteo geocoding 打中文原词是错的，实测：

| 输入 | 直接查的结果 |
|---|---|
| `东京` | 浙江一个 `PPL` 村（日本东京的 GeoNames 别名是繁体「東京」） |
| `纽约` | 完全 MISS |
| 任意 | 原 Nominatim 兜底分支 100% 连接超时，属于写在那里但跑不到的死代码 |

三级策略：**别名表 → 择优 → 拒答**。

1. `CITY_ALIASES` 把高频目的地映射成规范英文名 + 国家码（约 300 条，覆盖亚洲/欧洲/北美/中南美/大洋洲/非洲主要城市）。
2. 候选按 `feature_code`（首都/省会/普通居民点）**和人口**打分。人口权重必须高于行政级别——权重调反的那版里，"纽约"命中了内布拉斯加人口 7 千的 York。
3. 别名表知道国家时**只接受国家匹配的候选**；查不到就抛 `LocationUncertain`，不外推、不猜。表外地名标 `uncertain=True` 交给上层披露。

变音符号是第三个坑：`Reykjavik` 在 GeoNames 里是 `Reykjavík`，所以失败后依次重试「英文 → 中文规范名 → 原词」。

### 逐日天气（`get_weather_forecast`）

行程有几天就查几天，**一次请求拿整段**。Open-Meteo 本来一次就返回 16 天，按天各发一次请求属于白花配额。

- 入口：`get_weather_forecast(city, start_date, days≤16)`；provider 侧 `_fetch_once` 用 `days` 参数区分单日/整段。
- 进清单：逐日各一条 `天气（第N天 MM-DD）：阴，12~16℃（降水 40%）`，键都是 `weather`。用户能直接看出第 2、3 天到底查没查。
- 派生：装备按**整趟最冷那天**与**降水最高那天**判，不按第 1 天拍板；`facts.weather_summary` 写逐日摘要供提示词与装备规则关键词使用。
- 降级：`_load_weather` 先要 `weather_forecast` 依赖，没有或失败再退回单日 `weather_lookup`；两者都拿不到就写"未获取到可验证数据"，不猜温度。
- 出行服务那条链路（`travel_service.get_weather_context`）同样按 `requirements["days"]` 查整段，拿不到时给的是"禁止猜测温度"的明确状态。

### 实时外部事实：空气质量 / 汇率 / 节假日

天气之外还有四类"查回来就能进约束"的事实：前三类免 Key、走同一套 provider 抽象（缓存 + 熔断 + `fetched_at`），时差是纯本地计算（`zoneinfo`，不联网）：

| 工具 | 数据源 | 进清单的方式 | 拿不到时 |
|---|---|---|---|
| `get_air_quality(city, start_date, days≤5)` | Open-Meteo air-quality | 只有 AQI ≥ 100 的那天才写一条"减少长时间户外"；AQI 优良不占行 | 不写条目（补充证据，不制造噪声） |
| `get_exchange_rate(base, quote)` | Frankfurter（欧洲央行） | 境外目的地一条"1 人民币 ≈ X 日元"，软约束，不改预算口径 | 不写 |
| `get_public_holidays(country, year)` | Nager.Date | 行程日期落在法定假日时一条提示，软约束 | 不写 |
| `get_time_offset(city, date)` | `zoneinfo`（离线） | 与北京不同区时一条"时差：比北京时间早/晚 X 小时"，软约束 | 不写（不按经度粗算） |

三条边界：

- **只做事实，不做推荐**。汇率不替用户算"够不够花"，假日不替用户改行程，只把事实摆进清单。
- **货币表外一律不猜**（`travel_facts.currency_for`），境内目的地不写汇率。
- **节假日只认公共假日**，调休上班日不在接口里，不能拿来推"这天人多"。

#### 调用时机（谁调、什么时候不调）

| 事实 | 调用条件 | 跳过条件 | 失败 |
|---|---|---|---|
| 天气 | `destination` 非空，`days` 已知 | 目的地为空（derive 已提前反问） | 写"未获取到可验证数据"，不猜温度 |
| 空气质量 | 目的地非空且 `days ≤ 5` | 天数超接口窗口 | 不写条目 |
| 汇率 | `currency_for(destination)` 命中 | 境内 / 货币表外 | 不写 |
| 法定假日 | **出发日期明确**且境内 | 未给日期（按今天推会误报）/ 境外 | 不写 |
| 时差 | 目的地已解析出时区 | 境内 / 与北京同时区 | 不写 |

- **并发 + 单条限时**：`_load_facts` 用 `asyncio.gather` 并发取四条，每条 `asyncio.wait_for(..., FACT_TIMEOUT_SECONDS=4)`。之前是串行，provider 三重试时最坏两分钟，和"单次规划 ≤60s"冲突。
- **位置固定**：都在 `derive` 内、`plan` 之前。天气要进规划提示词，挪不到后面；其余三条只影响约束与装备，但也没必要多一层状态。
- **追问轮会重算**：`clarify` 复跑一次 `_load_facts`，所以 `enrich_*` 必须是"重写同名 key"而不是追加，否则同一天会出现两条。

### 活动数据（`get_local_activities`）

provider 只有一个：`Photon`（免 key，OpenStreetMap 场所清单）。**不做场次查询**——Ticketmaster 需 Key 且无中国业务，
秀动/大麦要签名或反爬，场馆官网是前端渲染。用户要参加具体活动时由用户在需求里指定，
走 `TripFacts` 的固定条目，不依赖 provider。

Photon 有四个必须知道的行为：

- **`q` 只做名称匹配**，`q=park` 返回的是叫 "Park" 的酒店和商场。按类别取景点必须用 `osm_tag`。类别分三组查（`_TAG_GROUPS`）：景点组 `tourism:attraction` / `tourism:museum` / `tourism:viewpoint`，自然组 `leisure:park` / `amenity:marketplace`，**可参与组** `amenity:theatre` / `amenity:arts_centre`。多个 `osm_tag` 之间是 OR，所以一组一次请求。加类别要同时改 `_TAG_GROUPS` 和 `_PLACE_KINDS`——只加前者不会报错，但结果会被中文名映射过滤成空。
- **可参与组必须单独成组**，不能和景点混一次查。混查时 `limit` 配额会被热门类别占满，实测上海返回全是景点和博物馆，`theatre` 一条不剩，于是"想参加活动"这个需求落空。截断时也要按 `participatory` 优先排序。
- 只带 `q=<city> <keyword>` 会让"名称里没有城市名"的本地场馆全部漏掉（圣托里尼全岛的希腊语馆名都不含 Santorini），所以改成 `q=<city>` + `osm_tag` + `lat/lon` 偏置 + 60 km 距离过滤。距离过滤是挡住"圣托里尼返回罗马尼亚博物馆"的那道闸。
- **噪声要过滤**（`_is_noisy_place`）。OSM 的 `tourism=attraction` 非常宽松，上海的返回里混进过 `цирк`（俄语标注）和 `Shanghai Evergreen Printing Machinery Co.`（印刷机械公司，工厂也被标成景点）。丢两类：公司/工厂式名称（` co.` / `ltd` / `factory` 等标记），以及非俄语系城市里的西里尔名。西里尔名不能一律丢——莫斯科的 `Палеонтологический музей` 是本地名，所以按 `country_code` 白名单判。

证据摘要按"能不能参加"分两段输出（`_activities_summary`）：`可以买票或报名参加的活动场所` 与 `另外可去的地方`。只列"可去之处"而不区分，模型会把景点当活动写进正文，用户也看不出两者差别。

两条硬约定：

- **绝不跨城市兜底**。旧实现在找不到 `<city>_activities.txt` 时兜底到 `outdoor_activities.txt`，于是"新加坡"拿到了千岛湖露营、黄山、泰山——把别的城市的资料冒充本地证据。现在只有该城市自己的文件才读得到，本地文件按拼音命名（`hangzhou_activities.txt`），中文城市名先经别名表转一次。
- **拿不到就说拿不到**。provider 失败返回 `available: False` + 原因，`travel_executor` 把它记成一条 `ok: False` 的工具调用，`travel.py` 的 `tool_fallback` 分支据此降级；不编场次、不编票价、不把场所清单写成"当天活动"。

`get_local_activities` 的输出里 `source` 会说明数据到底从哪来（`ticketmaster-discovery-v2` / `photon-osm` / `local_activities_file`），`kind` 区分 `event`（带日期票价）与 `poi`（只是可去之处）。票价是原始币种，不进预算。

### 证据与审核的三个边界

- **一天行程也要查活动证据**。原来 `days == 1` 时 `build_steps` 直接跳过活动证据，最短的行程反而最靠模型编。
- **本地资料解析只留一份**（`app/activity_notes.py`）。UI 目录页与工具原先各写一套正则，改一次格式得改两处。
- **反思快路径按行程天数判，不按证据条数判**（`can_use_deterministic_reflection`）。原先写死 `len(evidence_steps) == 1`，加上活动证据后一天行程变成两条，快路径永远走不到；但放宽成"证据干净就跳过"会让两天以上行程也跳过模型审核，实测一次挂 5 个测试——说明"多天必须模型审"这条保守边界是有意的。现在条件是 `days == 1` 且每条证据都 `completed`、无 `warning`、无 `stale`。

手工验证（会联网，不属于评测）：`python scripts/verify_external_tools.py`，
覆盖天气、路线、活动，以及东京/纽约/圣托里尼三个地名的解析正确性。

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

**生成器里的 `_descriptor()` 只决定"这件商品叫什么"**，不再承担"把内部装备名
翻译成商品词"的职责——装备派生（`trip_gear/gear.py`）已经直接使用商品词
（羽绒服 / T恤 / 登山鞋 / 雨伞 / 儿童）。这样新增商品只需要按品类命名，
不需要同步改一张装备名映射表；库里没有的品类，检索不到就不会出现在清单里。

## 记忆提取的位置

记忆提取**不在主图关键路径，也不自动跑**。回答结束后系统不做任何记忆判断，
只在对话区留一个「🧠 提取记忆」按钮（`render_memory_entry`），点了才提取。

历史上这里出过两次问题，都指向同一个事实——没人起跑就等于没有功能：

- `extract_memory` 曾挂成顶层节点、靠 `state["extract_memory"]` 判断，那个状态
  从来没被置真，是死分支，已删除；
- 删掉之后 UI 只剩"等 `memory_extraction` 事件 / 取缓存结果"，没有一处调
  `start`，于是每轮都落到"没有值得写入的内容"，界面上永远没有可勾选的入口。

### 提取窗口：游标决定"从哪开始"

`conversation_store.memory_extraction_cursor` 按 `thread_id` 记一条
`last_message_id`，表示这个会话的消息**已经提取到哪一条**。点按钮时：

1. `TurnServices.memory_extraction_window` 取游标之后的 `role == 'user'` 消息；
2. 按条数（20）与字数（4000，单条 800）双重封顶，`remaining` 告诉界面还剩多少；
3. `messages` 为空 → 直接返回"没有新消息"，**一次模型都不调**；
4. 提取成功后 `record_memory_extraction` 推进游标；失败不推进，这些消息还能重试。

游标只前进不后退（SQL 里 `MAX(last_message_id, excluded.last_message_id)`），
重复点击或并发都不会把同一段再送一遍。删会话时游标一起删。

窗口经 `TurnState.memory_window` 透传进记忆子图，节点不再自己翻整段历史——
token 花多少由窗口决定，不由历史长度决定。

### 没有逐字流式：正文一律整段到

`token` 事件还在协议里，但**没有 Agent 再发它**：

| Agent | 正文怎么出 |
|---|---|
| `trip_gear` / `travel` | 正文是模板一次算好的 Markdown，01–05 面板先出，原文折叠在「查看 Markdown 原文」 |
| `shopping` | 正文定稿（含一次校验/修复）之后再发 `answer_blocks`，卡片与正文**同时到** |
| `customer_service` | 同上：答案算完一次给全 |
| `general` | 只有一段正文（也包成 `text` 块），`ainvoke` 一次返回 |

理由：这些 Agent 的答案已经由结构化卡片/面板承担，逐字正文只会在卡片下方再刷一遍
同样的内容、结束再被版式替换。**"流式"现在只剩事件流**（阶段、分类、检索、工具、
中断），它们才是真正需要边跑边看的东西。

结构化结果也因此改成**算完再发**（`answer_blocks` 挪到 `validate` / 生成结尾）：
以前"卡片先出、正文后补"会让同一条回答分两段落地，看起来像刷了两次；
现在一次给全，和 travel 的 01–05 面板同一种手感。

### 界面上必须看得见的两件事

- **还有多少没提取**：按钮旁边一行说明，区分"还没提取过，将从头提取 N 条"与
  "自上次提取后有 N 条新消息"；这两句都只读库，不建图、不调模型。
- **上次点完是什么结果**：`last_memory_extract` 记 `candidates` / `no_content` /
  `no_new` / `timeout` / `error` / `done`，写在同一行说明里。

没开始对话（既没有会话消息、游标后也没有新消息）时按钮不渲染：空会话里摆一个
"提取记忆"只是噪声。

### 候选卡片：默认不写，一键写、再点就撤

有候选时 `render_memory_extraction_panel` 就地渲染一张卡片，**默认一条都不写入**：

- 每行左边一个按钮：未写入时是「写入」，点一下立即写进 MD + 向量；写成功后
  按钮变「已写入 · 撤销」，再点一次把这条删掉，行回到未写入，可以再写；
- 底部「全部写入 / 全部撤销」是同一个按钮的两种状态（全写完就翻成撤销），
  另有「全部忽略」（进 `rejected_memories`，之后不再提）与「收起」（只是关面板）。

写入走 `TurnGraph.save_memory_candidates` → `services.save_user_selected_memories`，
撤销走 `memory_repository.delete_memory`。这里**不再走 `interrupt` 审批图**：
按钮点击本身就是确认，图往返只会让"点一下写入"变成两次跳转。`MemoryApprovalGraph`
因此只剩测试在用，保留与否待定。

候选属于哪个会话由 `memory_extraction_thread` 记住：切到别的会话不显示，切回来
还在——没处理完的候选不会因为继续对话而消失。

去重有三层，缺一层都会重复提问：游标（哪些消息送过）、`existing_contents`
（MD 里已有的内容）、`rejected_memories`（用户拒过的，作为 `excluded_contents`）。

### 一个必须留着的静态方法

`TurnServices._latest_user_message` 只取本轮用户原话：助手回答和更早的轮次
都不该成为事实来源。它曾经被调用却从未定义，让整条提取链路每次都以
`AttributeError` 失败——改记忆相关代码时先确认它还在。

### 商品检索：2-gram 索引

`ProductRepository.search` 原来用 `LIKE '%词%'`——前导通配符走不了索引，
是**全表扫描**。商品上十万行之后，这是最先崩的一层（目录摘要不受影响，
它按品类聚合）。

现在优先走 FTS5 的 2-gram 倒排（`app/retrieval/search_index.py`）：

- **为什么不是 trigram**：trigram 只索引 3 字以上片段，"帐篷""睡袋""T恤" 查不到；
- **为什么不是 unicode61**：它不切中文，整段商品名会变成一个 token；
- **为什么不用 jieba**：不引新依赖。建索引时把中文按相邻两字切开
  （`双人帐篷` → `双人 人帐 帐篷`），查询用同样切法，多词之间隐式 AND。
- **两字词是精确的**（单个 2-gram 就是该子串），三字以上是近似匹配，
  再补一条 LIKE 复核；单字词没有 2-gram，整条查询退回 LIKE。

索引不可用（旧 SQLite 没编译 FTS5、行数超过自动构建阈值）时静默退回 LIKE，
检索不中断。

```bash
python scripts/build_product_index.py          # 建/重建
python scripts/build_product_index.py --stats  # 只看状态
```

行数 ≤ 5 万时首次检索会自动建索引；超过就要求先跑上面的脚本（不在请求路径里
做全量构建）。实测 20 万行：LIKE 32ms → 索引 8ms，建索引 1.3s。
`tests/test_search_index.py` 钉住"索引结果必须与 LIKE 完全一致"。

## 事件契约

custom chunk 与旧 SSE 一致，UI 无需改动：

```json
{"event": "token", "data": {"token": "你"}, "step": "generate", "timestamp": 0.0, "trace_id": "..."}
```

回答块：`{"event": "answer_blocks", "data": {"agent": "...", "blocks": [...]}}`。

分类结果事件（`classify`）带 `method` / `strength` / `keyword_hits` / `keyword_scores`：

- `method`：`keyword`（关键词直通）/ `context`（沿用上一轮）/ `llm`（小模型）/ `llm_arbitration`
  （低置信仲裁）/ `llm_repair`（输出格式异常后修复）/ `policy`（场景策略纠正）；
- `strength`：`strong` / `weak` / `none`，说明关键词层够不够定案；
- `keyword_hits`：命中的词，按意图分组（`{"shopping": ["跑鞋", "推荐"], "travel": ["杭州"]}`，
  弱购物词单列 `shopping_weak`）；`keyword_scores` 是各意图得分，判定线 1.2。
- 这几项**只用于展示**，不参与任何判定：判定仍看 `intent` / `route` / `retrieval`。

同一个阶段事件（`step`，第 1 步）在分类结束时被回写一次，描述里写"实际怎么定案的 +
选了哪条检索 + 命中几个词"。关键词直通的轮次不该显示"小模型正在分析"，那是没发生的事。
块类型 `text/lead/stats/cards/table/timeline/panel` 由 `app/ui/blocks.py` 从真实数据构造
（商品行、订单行），前端 `render_answer_blocks` 直接渲染，不解析 Markdown。
trip_gear / travel 仍用 `plan_result` 渲染 01–05 面板。

`done` 额外带 `timing_ms`（首 token / 回答就绪 / 链路）和 `usage`（按本轮 `trace_id` 从 TraceRecorder 汇总的 prompt / completion / total token）。供应商不回传 usage 时该字段缺省；当前界面不展示 Token（实测供应商两轮都没回传，摆了也只能显示「—」）。

子图事件靠 `subgraphs=True` 透传，命名空间在 `_split_chunk` 里剥掉。结构化结果单发 `plan_result` 事件（`facts` / `constraints` / `itinerary` / `gear` / `products` / `issues` / `verdict` / `revision`），前端据此渲染面板，不必解析 Markdown。

## 过程展示：每一轮在干什么都要看得见

过程区由 `app/main.py` 的 `_process_events_html` / `_fmt_event_html` 渲染成统一的卡片，
四类：`classify`（意图与检索）、`step`（阶段）、`tool_call`（工具）、`error`。
流式期间 `include_transient=True`，思考事件作为"当前动作"跟着走；结束之后只留持久事件，
默认折叠成「运行过程 · N 条」——正文优先，证据留在可展开处。

分类卡片固定四行，一行说一件事：

| 行 | 内容 | 为什么在这 |
|---|---|---|
| 意图 · 策略 | `购物(shopping) · 单步` | 选哪个 Agent、走 ReAct 还是先规划 |
| 检索 · 子意图 | `混合 · 商品属性 + 评价/攻略` | "选了 RAG 还是 SQL"一眼可读 |
| 判定 · 置信度 | `关键词直通（强证据） · 0.85` | 说清楚这次到底调没调模型 |
| 命中关键词 | 按意图分组的词 + 各意图得分 + 理由 | 追问"它凭什么判成购物"的答案 |

**每个阶段卡片写的是"这一步的结论"，不是"进行中"三个字**：完成后描述里带着决定与数字
（`预算 8000 元分成 行程 7200 / 装备 800`、`已生成 5 天行程（广州）· 住宿 ¥1,200 + 城际 ¥480（估算）`、
`行程 ¥2,180 / 预算 ¥8,000 · 发现 0 个问题 · 结论 feasible`）。停在追问或等比例时，
状态是 **待确认**（`waiting`）而不是进行中/已完成——`clarify` 与 `budget_split` 在
`interrupt()` 之前各播一条 `waiting`，写明在等哪几项。

**过程记录跨"追问/等比例"这一段延续**：一轮对话被 `interrupt` 切开时会分成几段，
每段都往同一个事件列表后面接（`run_streaming_realtime(seed_events=…)`）。上一段存在
`pending_clarify["events"]` 里，续跑时先铺回去，并从那条追问消息上摘掉——否则每答
一次卡片就从零开始，看起来"步骤全没了"，而新一段只剩 02、03 两行同时在跑。
过程区的展开有一条简单规则：**还在等用户回答（`pending_clarify` 在屏幕上）时，最近一轮展开；
回答完就收起成一行**。等的时候折起来，用户看到的是"步骤全没了"；答完还摊着，又压住正文。
更早的轮次一律折叠。

**每个阶段先报"在干什么"，再报"做完了"**：`_progress()` 在节点开头发一条
`status="active"` 的 step + 一条 thinking（"🧠 正在生成 3 天逐日行程（杭州）"、
"正在查天气、空气质量、汇率、节假日与时差"、"正在核对 11 条约束与预算、时间冲突"），
`_notify()` 在结尾发同编号的 `completed`。之前只有结尾那一条，排行程、挑款、
校验这几秒到几十秒里过程区是死的，只剩一句通用兜底（"正在生成并校验回答"）——
那不是在报进度，是在重复阶段名。

三条硬约定：

1. **人话词表只有一份**：`app/streaming.py` 的 `ROUTE_LABELS` / `INTENT_LABELS` /
   `RETRIEVAL_LABELS` / `RETRIEVAL_SCOPE` / `CLASSIFY_METHOD_LABELS`，过程卡片和
   第 1 阶段的描述文案共用。事件里存内部枚举，界面负责翻译；两边各写一份必然漂移。
2. **"正在干什么"只挂一条**：取最近一次阶段切换之后的思考或未完成工具调用
   （`_latest_action`）。阶段一换，上一段的"小模型正在分析…"就过期了；没有可展示的
   动作时按阶段兜底一句（`_DEFAULT_LIVE_ACTIONS`），不让卡片亮着却不说在干什么。
3. **入库前先摘掉 token/thinking**：`turn_events_for_storage` 先过滤 token（逐字一条）、
   thinking、done/reset，再看 256KB 上限；超了才退化成只留 `plan_result`。
   不先过滤的话，一轮 5 天行程的 token 就有几百 KB，一入库就触发降级，
   历史回看只剩四行默认阶段——"追问之后过程全没了"的真正原因。
4. **链路阶段按节点名认，不按 total 认**：travel 的 total 也是 4，和旧四阶段同数，
   只凭 `total > 4` 判断会把 travel 误判成旧模板，补出"资料检索与准备 / 生成并审核回复"
   这些根本没发生过的阶段。只要任一 `step` 事件来自图节点（`derive`/`plan`/`verify`…），
   就按链路渲染，只画真实发生过的阶段。
5. **分类卡片在行程链路里也要保留**：多阶段时间线（8 步/4 步）以前只输出 step 列表，
   `classify` 事件按别名归到第 1 阶段后被整块丢掉——"选了 RAG""命中了哪些关键词"
   全都看不见，只剩阶段名。现在多阶段分支按原始顺序输出，非 step 事件原样保留；
   分类那一步自己的编号（4 步制）不再单独占一行，避免出现两个"01"。
   收尾渲染（`include_transient=False`）会把残留的"进行中"收成"已完成"：
   阶段在中断处停下时，它开始时那条 active 事件会留在流里，用户会以为卡住了。
6. **逐日行程里必须有住宿与大交通**：住宿每天一行（最后一晚没有），大交通若模型
   没写进条目，就在第 1 天挂一行"城际交通（估算）"并标明往返——两块的金额之和
   等于面板合计，逐日小计加起来也对得上。
7. **工具卡片写查询、命中数和耗时**：`rag` / `nl2sql` 的 `input` 是查询本身
   （`查询「杭州 三天 攻略」`），`output` 是 `命中 N 条`，另带 `elapsed_ms`。
   以前写"返回 2 条文档"，看着像结论而不是查询，用户没法判断这次检索到底查了什么。

这两项都是**只读展示**：展示层拿不到的字段缺省即可，绝不因为缺字段改变检索或判定。

## 评测

场景集 `data/evaluation/trip_gear.spec.jsonl`，一行一个场景；`expect` 是**人工标注**，不是被测代码算出来的。`products` 内联，评测不依赖数据库当时状态。

```bash
python scripts/eval_trip_gear_specs.py     # 输出 data/evaluation/trip_gear.spec.report.json
python scripts/eval_nlu_specs.py           # NLU 场景集，不符合时退出码 1
```

NLU 场景集 `data/evaluation/nlu.spec.jsonl` 覆盖关键词层、槽位、装备识别，
`expect` 是**人写的期望**（不是跑出来再抄回去），改词表后先跑这个。
它只测确定性部分：小模型、仲裁、槽位补全不在覆盖范围。

关键指标：`new_accuracy` / `legacy_accuracy`（对照"只查理由里有没有'温标'"的旧逻辑）、`legacy_reported_insufficient`（旧逻辑恒为 0）、`legacy_false_accept`、`spec_unknown_rate`。评测确定性：不调模型、不联网，同一份场景集结果一致。

## 新增一个 Agent

1. 定义状态：通用字段放 `app/graphs/state.py`，该场景专有的就近写在子图模块里。
2. 写节点，签名 `(state, runtime: Runtime[TurnContext])`，事件用 `runtime.context.sink_for(runtime)`。
3. `build_xxx_graph()` 里用 `retry_policy` / `timeout` 声明策略。
4. 在 `app/agents/specs.py` 加一条 `AgentSpec`，`builder` 写成 `"模块:函数"` 字符串。
5. 在 `app/tools/agent_capabilities.py` 加一条 `AgentToolPolicy` 白名单。

**就这两处**。顶层图的节点、`ROUTE_TARGETS`、配置页都从 `AGENT_SPECS` 派生。
`builder` 之所以是字符串而不是函数引用，是为了让 `scripts/build_pipeline_spec.py` 的
静态解析（只读 AST、不 import 业务模块）也能读出 Agent 与子图的对应关系。

点名叫某个 Agent 时（脚本/评测）走 `state["forced_agent"]`，分类照跑但入口以点名为准。

### 启停与配置

- 开关落 `data/agent_config.json`（生成物，不进版本库），`app/agents/config.py` 读写；
- `nodes.route()` 末尾兜底：被停用的 Agent 不接客，回退 `general`，点名调用也不放行；
- UI「Agent 配置」页（`app/ui/agents_page.py`）列出每个 Agent 的工具面、自然语言入口与输出样式，可直接开关；
- **工具也可以自己加、自己选**：把 `TOOLS = [@tool ...]` 写进 `data/tools/*.py`，
  启动时并入全局工具池；「Agent 配置」页每个 Agent 一个多选框，保存后
  `get_agent_tool_policy()` 用配置替换默认白名单（可加可减），注册表只注册选中的。
  `data/tools/` 是本地代码，只放自己写的文件。

**不做对外服务**：没有 MCP / HTTP 入口，也没有 Agent 的导入导出。可插拔只指"加一条 `AgentSpec` + 一条白名单就能接进来，开关控制是否接客"；
Agent 的编排与工具都写在仓库里，因为它们依赖 `TurnContext` / `services` / 数据源，单独打包出去跑不起来
（实测每个子图的依赖闭包约 86–88 个模块）。

依赖注入：生产从 `services` 取能力；测试用 `TurnContext.extra` 覆盖 `planner` / `product_search` / `weather_lookup` / `memory_loader` / `max_revisions` 等。

## 文件速查

| 文件 | 职责 |
|---|---|
| `nlu/numerals.py` | 中文数字与金额词法还原 |
| `nlu/lexicon.py` | 唯一一份词表 |
| `nlu/slots.py` | 槽位抽取（两套模型共用） |
| `nlu/signals.py` | 意图打分与路由判定 |
| `tools/geo.py` | 全球地名解析：别名表 + 择优 + 保守拒答 |
| `activity_notes.py` | 本地活动资料解析（UI 目录页与工具共用同一份） |
| `tools/providers.py` | 天气/路线/活动 provider、缓存、熔断 |
| `tools/time_offset.py` | 城市 → IANA 时区、UTC 偏移、与北京时差（离线） |
| `agents/specs.py` | Agent 清单：节点名 / 能力面一处定义 |
| `agents/config.py` | Agent 启停（`data/agent_config.json`） |
| `ui/agents_page.py` | Agent 配置页：工具面、启停开关 |
| `ui/blocks.py` | 回答块构造（只读真实数据，不造字段） |
| `retrieval/search_index.py` | 商品 2-gram 检索索引（FTS5） |
| `trip_gear/catalog.py` | 商品库目录摘要（按品类，给模型看） |
| `evaluation/nlu_spec.py` | NLU 场景集加载与评测 |
| `services/slot_assist.py` | 缺槽位时的小模型补全（默认关闭） |
| `trip_gear/constraints.py` | 约束派生（规则优先、模型补充） |
| `trip_gear/itinerary.py` | 逐日行程、时间校验、无模型兜底、住宿估价 |
| `trip_gear/gear.py` | 装备需求推导规则 |
| `trip_gear/owned.py` | 已有装备判定 + 规格抽取 |
| `trip_gear/spec_match.py` | 规格匹配三态 |
| `trip_gear/shopping.py` | 缺口 → 商品检索 |
| `trip_gear/purchase.py` | 采购可履约 + 替代链 |
| `trip_gear/estimates.py` | 机票/住宿估算（固定单价 + 口径说明） |
| `trip_gear/verify.py` | 确定性校验（约束/齐全性/规格/可履约） |
| `trip_gear/verdict.py` | 四态结论 + 修复方案 + 信息缺口 |
| `trip_gear/repair.py` | 修复动作与落地 |
| `trip_gear/tradeoff.py` | 统一代价 |
| `trip_gear/spec_eval.py` | 规格评测与改前/改后对照 |
| `trip_gear/render.py` | Markdown 展示 |

`docs/generated/` 放生成物，**不要手改**：`pipeline-spec.html/json` 由 `smartlife-agent/scripts/build_pipeline_spec.py` 生成；`current-agent-flow.html` 是早期的分支流程快照，只作查证。
