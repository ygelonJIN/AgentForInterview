"""行程/装备协同规划的数据结构。

刻意保持字段少而稳定：
- 约束 4 个字段：内容、来源、硬度、状态
- 行程条目 4 个字段：时间、活动、时长、花费
- 装备条目 4 个字段：名称、数量、理由、状态

新增场景不需要新增字段；场景只是行程条目的自然语言描述。
"""
from __future__ import annotations

from typing import Any, List, Optional

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


class Itinerary(BaseModel):
    destination: str = ""
    days: List[DayPlan] = Field(default_factory=list)


class GearItem(BaseModel):
    """装备清单中的一条。status: owned / need_buy / self_provided / skipped。"""

    name: str
    qty: int = 1
    reason: str = ""
    status: str = "need_buy"


class OwnedItem(BaseModel):
    """从对话或长期记忆中解析出的已有物品。"""

    name: str
    qty: int = 1
    source: str = "对话"


class Issue(BaseModel):
    """校验发现的问题。"""

    constraint: str
    actual: Any = None
    limit: Any = None
    hint: str = ""
    severity: str = "critical"


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
