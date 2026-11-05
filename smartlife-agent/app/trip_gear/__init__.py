"""行程约束派生与装备协同规划（trip gear）。

这个包只负责一条链路：
    一句需求 -> 约束清单 -> 逐日行程 -> 装备清单 -> 已有/缺口 -> 商品 -> 校验

它不重写检索、记忆和审批，只复用现有基础设施。
"""
from app.trip_gear.constraints import derive_constraints, enrich_weather
from app.trip_gear.gear import derive_gear
from app.trip_gear.intent import is_gear_request
from app.trip_gear.models import (
    Constraint,
    DayPlan,
    GearItem,
    Issue,
    Itinerary,
    ItineraryItem,
    OwnedItem,
    Requirement,
    TripFacts,
    Verdict,
)

__all__ = [
    "Constraint",
    "DayPlan",
    "GearItem",
    "Issue",
    "Itinerary",
    "ItineraryItem",
    "OwnedItem",
    "Requirement",
    "TripFacts",
    "Verdict",
    "derive_constraints",
    "derive_gear",
    "enrich_weather",
    "is_gear_request",
]
