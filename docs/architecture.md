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

装备类请求（`sub_intent=gear_plan`）与多天行程规划（`sub_intent=plan_trip` + `plan_and_execute`）不进并行分支，走 `trip_gear` 子图，见下文「行程子系统」。

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

`classify` 输出四个信号，各管一件事：`intents`/`primary_intent` 选 Agent，`route` 选 ReAct 还是 Plan&Execute，`retrieval` 选 SQL/RAG/混合，`sub_intent=gear_plan`（或纯行程的 `plan_trip + plan_and_execute`）进 `trip_gear`。多意图最多保留两个，只有 `shopping + travel` 走并行分支。

`trip_gear` 同时接管多天行程规划（`nodes._is_trip_plan`）：01–05 面板不是可以贴到任何回答上的样式，它要的是约束/行程/装备/预算这套结构化结果，而 `travel` 子图只产出正文。收敛到同一张图，用户才不会遇到"同样是规划，有时有面板有时只有一段话"。

判定顺序与阈值（`app/nlu/signals.py`）：

| 层 | 规则 |
|---|---|
| 关键词打分 | 品类/品牌 1.0、购物动作 0.6、泛化词 0.4；地名 1.0、强旅游词 1.0、场景词 0.6、时长 1.0、地名+出行语境 0.4。跨度去重（"跑步鞋"只算一次），否定词（"不要帐篷"）直接扣掉 |
| 购物就绪 | 必须命中**实义品类/品牌**且总分 ≥1.2；只有"价格/多少钱"不算 |
| 旅游就绪 | 总分 ≥1.2；`route` 由规划词或"玩 N 天"决定 |
| 复合 | 两类都就绪 → 取分数最高的两个，`confidence=0.9` |
| 低置信 | 关键词层返回 `None` → 指代沿用 → 小模型（<0.55 分或 JSON 坏）→ 主模型仲裁一次 |
| 模型失败 | 有关键词证据就按关键词兜底（`confidence=0.4`），没有才回 `general` |

`route` 只有三个消费方：`general`（react / general_plan）、`travel`（chat / 规划图）、`composite`（写死 `plan_and_execute`）。`shopping`、`trip_gear`、`customer_service` 不看它。`travel` 的 `plan_trip` 是例外：它带着 `plan_and_execute` 时被改派到 `trip_gear`。

`sub_intent=gear_plan` 的劫持改成显式枚举 + 中文限定词（`nodes._is_gear_plan`），不再用 `"gear" in sub_intent` 的包含判断。

## NLU 层（`app/nlu`）

一句话只解析一次，三处共用，这是"同一句话在两条链路抽出两个值"的根治点。

| 文件 | 职责 |
|---|---|
| `numerals.py` | 中文数字按位值组合还原（三千五=3500、一万五=15000、十二万=120000），金额连单位切分 |
| `lexicon.py` | **唯一**一份词表：品类、品牌、旅游词、场景词、地名、客服词、检索需求词、否定词 |
| `slots.py` | 目的地、天数、人数、预算、日期、住宿、交通、同行儿童 |
| `signals.py` | 意图加权打分 + route 判定 + 兼容旧调用点的谓词 |

三条硬约定：

1. **`SharedConstraints` 与 `TripFacts` 都从 `slots.extract_slots` 取值**，不再各写正则。
2. **`people=None` 表示"用户没说"**，计算按 `effective_people`（=1）处理，展示写"未指定（按 1 人计）"，不能伪装成用户说了 1 人。
3. **预算先剔除装备预算和商品单价**，再按"总预算 > 上限 > 下限 > 唯一金额"取；"装备预算1000，总预算3000"取 3000，"预算3000，手机5000元"取 3000。

日期解析需要"今天"时由调用方注入（`extract_slots(text, today=...)`），保证评测可复现。

### 槽位补全（可选，默认关闭）

规则抓不到的写法（"去石塘待几天"）由 `app/services/slot_assist.py` 兜一次：
`derive` 与 `extract_requirements` 在**缺必填槽位时**才调用，超时 4.5s。

- 开关：`SMARTLIFE_SLOT_ASSIST=1`。默认关，因为"猜错目的地"比"多问一句"代价大。
- 只填空缺，不覆盖规则结果；结果来源标 `模型补全`，约束清单里看得见。
- 防幻觉：目的地必须能在原话里找到；天数 1~60、人数 1~100、预算 >0 才收，
  过期的出发日期直接丢。

## 行程子系统（`app/graphs/subgraphs/trip_gear.py`）

### 链路

```text
derive → plan → gear → owned → shop → verify ─┬→ repair（自动修复）→ verify
   └→ clarify（缺目的地/天数）                ├→ verify_clarify（带代价的提问，≤2 轮）→ verify
                                              └→ finalize（不可行：替代方案 + 放弃清单）
```

两个 `clarify` 分工不同，**不要合并**：`derive` 后那个补"能不能开始规划"的槽位；`verify` 后那个补"方案到底行不行"的判断依据，只有算过一遍方案才知道该问什么。

### 出发日期与默认值

出发日期和人数同级，都是"有就用、没有就默认、默认要说出来"：

- 用户说了日期 → 约束清单写 `出发日期：2026-11-14`，来源"用户原话"；没说 → 写 `出发日期：未指定（按今天 2026-11-07 计）`，来源"默认规则"、软约束。
- **不能只写进 `facts` 不写进清单**。早先就是这样，于是用户明明说了日期，清单里也看不见；而校验器又拿不到日期，逐件刷"没有出发日期"。
- **没有日期时不判采购时效**：拿"估计的今天"逐件算到货，会把每件待购都判成赶不上，结论直接 `infeasible`，还会把真正的冲突挤出 05 面板。日期落地（或用户改期）后再判。
- 面板末尾固定一句"默认值：…。如果和实际不符，回一句我重排"，只列真正用了默认值的项。
- `ensure_start_date` 是**重写**不是追加：追问里补的日期要替换掉占位那条，否则同一个 key 会并存两条。`_merge_facts` 也必须合并 `start_date`，不然用户补答的日期会被静默丢掉。

01–05 面板只做展示：约束的 `hard`/`status` 仍参与判定（verify 判 critical、tradeoff 算权重），只是不再各占一列。

状态字段：`facts`、`constraints`、`itinerary`、`gear`、`owned`、`products`、`issues`、`revision`、`repair_options`、`verdict`、`clarify_rounds`。

### 用户指定的固定活动

活动证据只有**场所**（剧院、博物馆、公园），拿不到"某天演什么"——Ticketmaster 要 Key 且无中国业务，
秀动/大麦要签名或反爬，场馆官网是前端渲染。所以场次级信息不查，改由用户提供。

两条输入，走两条不同的路：

| 输入 | 落到哪 | 效果 |
|---|---|---|
| "已订好第 2 天 19:30 的陶艺工作坊，200 元" | `TripFacts.fixed_events`（`status="booked"`） | 钉进那天，其它安排让路，票价进预算 |
| "想参与一点活动" | `TripFacts.wants_activity=True`（软约束） | 只影响行程取向，**不产生任何日程** |

`apply_fixed_events` 的三条硬规则：

- **只钉 `booked`**。`planned` 只是"想去"，排不下就算了，不能挤掉别的安排。
- **没说是第几天就不钉**（`day=0`）。猜一天等于替用户决定哪天去。
- **让路的是可调整条目**。当天超时就从末尾删非固定条目；那天原本排了返程而活动在它之后，
  删的是返程——固定活动是用户已经买票的外部约束，删它等于替用户退票。

抽取上的坑：`slots.parse_fixed_events` 必须两道闸同时成立才认——已订语气（已订/买票/预约/报名）
**且**名字里有活动类型词（音乐会/工作坊/展览……）。只留一道闸会出事："想去上海玩两天"会被
抽成一个叫"上海玩两天"的活动，"想参加一点活动"会被抽成一个叫"一点活动"的日程。
语气词还要按长度降序匹配，"已订好陶艺工作坊"不能被"已订"抢走而剩下"好陶艺工作坊"。

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

三条硬规则：**搜不到就不进清单**（库里没有的不列）、**无货不当候选**、
**同类替代要标注**（"羽绒服"搜到"轻薄羽绒外套"要写"这是按羽绒服检索到的同类款"）。

### 选品：打分兜底 + 模型挑款

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

记忆提取**不在主图关键路径**：`finalize` 之后由 UI 调 `MemoryExtractionRunner`
单独跑（`app/graphs/memory.py`）。原先把 `extract_memory` 挂成顶层节点并判断
`state["extract_memory"]`，但那个状态从来没有人置真，属于死分支，已删除。

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

子图事件靠 `subgraphs=True` 透传，命名空间在 `_split_chunk` 里剥掉。结构化结果单发 `plan_result` 事件（`facts` / `constraints` / `itinerary` / `gear` / `products` / `issues` / `verdict` / `revision`），前端据此渲染面板，不必解析 Markdown。

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
| `nlu/numerals.py` | 中文数字与金额词法还原 |
| `nlu/lexicon.py` | 唯一一份词表 |
| `nlu/slots.py` | 槽位抽取（两套模型共用） |
| `nlu/signals.py` | 意图打分与路由判定 |
| `tools/geo.py` | 全球地名解析：别名表 + 择优 + 保守拒答 |
| `activity_notes.py` | 本地活动资料解析（UI 目录页与工具共用同一份） |
| `tools/providers.py` | 天气/路线/活动 provider、缓存、熔断 |
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
| `trip_gear/verify.py` | 确定性校验（约束/齐全性/规格/可履约） |
| `trip_gear/verdict.py` | 四态结论 + 修复方案 + 信息缺口 |
| `trip_gear/repair.py` | 修复动作与落地 |
| `trip_gear/tradeoff.py` | 统一代价 |
| `trip_gear/spec_eval.py` | 规格评测与改前/改后对照 |
| `trip_gear/render.py` | Markdown 展示 |

`docs/generated/` 放生成物，**不要手改**：`pipeline-spec.html/json` 由 `smartlife-agent/scripts/build_pipeline_spec.py` 生成；`current-agent-flow.html` 是早期的分支流程快照，只作查证。
