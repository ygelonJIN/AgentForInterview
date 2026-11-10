"""行程/装备协同规划的数据结构。

刻意保持字段少而稳定：
- 约束 4 个字段：内容、来源、硬度、状态
- 行程条目 4 个字段：时间、活动、时长、花费
- 装备条目 4 个字段：名称、数量、理由、状态

新增场景不需要新增字段；场景只是行程条目的自然语言描述。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Constraint(BaseModel):
    """一条被识别或推导出来的约束。内容自由填写，字段固定。"""

    text: str
    source: str = "用户原话"
    hard: bool = True
    status: str = "ok"  # ok / violated / relaxed / unverified
    # 机器可校验的身份标识，供校验器派发；空串表示只做展示、不参与自动校验。
    key: str = ""


class ItineraryItem(BaseModel):
    """逐日行程中的一个安排。"""

    time: str = ""
    activity: str
    duration_min: int = 0
    cost: float = 0.0


class DayPlan(BaseModel):
    day: int
    # 多城市行程里这一天在哪个城市；单城市行程留空
    city: str = ""
    items: List[ItineraryItem] = Field(default_factory=list)
    weather: str = ""
    lodging: str = ""
    # 当晚住宿费用（估算）。不给这个字段，"换住宿"这个动作就算不出钱，
    # 只能拿装备省下的钱和空气比大小。
    lodging_cost: float = 0.0


class Itinerary(BaseModel):
    destination: str = ""
    days: List[DayPlan] = Field(default_factory=list)


class GearItem(BaseModel):
    """装备清单中的一条。status: owned / need_buy / self_provided / skipped。

    ``specs`` 存这件东西的规格（温标、容量等），来源可能是商品库、用户
    自述或解析出的描述文本；空字典表示"规格未知"，由规格核对单独处理。
    """

    name: str
    qty: int = 1
    reason: str = ""
    status: str = "need_buy"
    specs: Dict[str, Any] = Field(default_factory=dict)


class OwnedItem(BaseModel):
    """从对话或长期记忆中解析出的已有物品。"""

    name: str
    qty: int = 1
    source: str = "对话"
    specs: Dict[str, Any] = Field(default_factory=dict)


class Requirement(BaseModel):
    """方案要成立就得满足的一条需求；装备与行程之间的接缝。

    ``triggered_by`` 指回触发它的行程条目（``day2-item3``）或约束
    （``constraint:c7``）。有这个指针，"停掉第二晚露营"才能顺带停掉
    "温标睡袋"，装备也才有资格回压行程——否则只能单向推导。

    ``weight`` 由 ``tradeoff`` 计算后填入，本类不自己推导，避免两处口径。
    """

    id: str
    kind: str = "gear"  # gear / capability / info
    subject: str
    triggered_by: List[str] = Field(default_factory=list)
    weight: float = 0.0
    satisfied_by: str = ""  # owned / self_provided / purchase / action_change
    delta: float = 0.0
    status: str = "unknown"  # ok / violated / unknown
    hint: str = ""


# 校验结论的四种状态。顺序有意义：越靠后越需要外部介入。
VERDICT_STATES = ("feasible", "repairable", "needs_input", "infeasible")


class Verdict(BaseModel):
    """一次校验的结论：不是问题清单，而是"接下来该走哪条路"。

    - feasible：硬约束全满足，直接输出
    - repairable：有代价可接受的改法，交给修复
    - needs_input：改不动，但问一句就能降代价，交给澄清
    - infeasible：改不动也问不出，输出替代方案与放弃清单
    """

    state: str = "feasible"
    blocking: List[str] = Field(default_factory=list)
    terms: List[Dict[str, Any]] = Field(default_factory=list)
    best_cost: float = 0.0
    tau: float = 0.15
    # 代价是否低到可以不经用户确认就动手；与"可不可行"是两个问题。
    auto_applicable: bool = False
    ask: Optional[Dict[str, Any]] = None
    alternatives: List[Dict[str, Any]] = Field(default_factory=list)


class Issue(BaseModel):
    """校验发现的问题。

    ``alternatives`` 是"这条问题有出路"时给出的候选，例如到货赶不上时可换的
    现货款。空列表表示没有找到补救方案，而不是"不用管"。
    """

    constraint: str
    actual: Any = None
    limit: Any = None
    hint: str = ""
    severity: str = "critical"
    alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    # 这条"还不知道"是否有可能推翻结论。True 才值得打断用户去问，
    # 否则问了也只是满足好奇心，还把一轮对话浪费掉。
    blocking_risk: bool = False


class FixedEvent(BaseModel):
    """用户点名要参加的固定活动（演出、课程、展会）。

    这是行程唯一会被"外部约束钉死"的来源。``status`` 分三档：

    - ``booked``：已订票/已预约，必须排进对应那天，其它安排让路；
    - ``appointment``：约好见人（"有一天去浦东找朋友"），地点和事由固定，
      日期可调——哪天排由排程决定，但不许不排；
    - ``planned``：只是想去，排得下就排，不挤掉别人的安排。

    分档是照着 `docs/关键决策总表.md` 里"硬度"的口径来的——用户说"已买票"
    和"想去看看"的约束力完全不同，混成一档会让系统擅自删掉别的日程。
    """

    title: str
    day: int = 0            # 第几天；0 表示没定位到具体哪天
    date: str = ""          # YYYY-MM-DD，有出发日期时可校验
    start: str = ""         # HH:MM
    duration_min: int = 120
    cost: float = 0.0
    venue: str = ""
    status: str = "planned"  # booked / appointment / planned
    source: str = "用户原话"


class TripFacts(BaseModel):
    """整趟出行的已知事实；装备规则只读这些，不读条目标签。

    ``people`` 为 ``None`` 表示用户没说人数——计算时按 1 人处理，
    但不能把它当成"用户说了 1 人"，展示和追问要区分这两种情况。
    """

    origin: str = ""
    destination: str = ""
    # 多城市串联（"广州、深圳、珠海"）：按顺序均分到各天
    cities: List[str] = Field(default_factory=list)
    days: Optional[int] = None
    people: Optional[int] = None
    budget: Optional[float] = None
    # 每晚住宿预算（用户说"酒店一晚不超400"时才有值），与整趟预算分开记
    lodging_budget: Optional[float] = None
    gear_budget: Optional[float] = None
    budget_scope: str = "all_in"  # all_in / trip_only
    start_date: Optional[str] = None
    lodging: str = ""  # camp / hotel
    transport: str = ""  # flight / train / drive
    with_kids: bool = False
    min_temp_c: Optional[float] = None
    rain_probability: Optional[float] = None
    uv_index_max: Optional[float] = None
    aqi_max: Optional[float] = None
    weather_summary: str = ""
    # 目的地时区：只有境外/跨时区行程才有值，境内留空（不写"时差 0 小时"）
    timezone: str = ""
    utc_offset: Optional[float] = None
    time_diff_hours: Optional[float] = None
    # 用户点名的固定活动；booked 会钉进行程，planned 只是偏好
    fixed_events: List[FixedEvent] = Field(default_factory=list)
    # 用户表达了"想参加活动"的意向（没说具体哪场），影响行程内容取向
    wants_activity: bool = False

    @property
    def is_camping(self) -> bool:
        return self.lodging == "camp"

    @property
    def effective_people(self) -> int:
        """计算用人数：没说就按 1 人。"""
        return self.people or 1
