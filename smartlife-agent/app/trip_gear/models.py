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


class TripFacts(BaseModel):
    """整趟出行的已知事实；装备规则只读这些，不读条目标签。"""

    destination: str = ""
    days: Optional[int] = None
    people: int = 1
    budget: Optional[float] = None
    gear_budget: Optional[float] = None
    budget_scope: str = "all_in"  # all_in / trip_only
    start_date: Optional[str] = None
    lodging: str = ""  # camp / hotel
    transport: str = ""  # flight / train / drive
    with_kids: bool = False
    min_temp_c: Optional[float] = None
    rain_probability: Optional[float] = None
    weather_summary: str = ""

    @property
    def is_camping(self) -> bool:
        return self.lodging == "camp"
