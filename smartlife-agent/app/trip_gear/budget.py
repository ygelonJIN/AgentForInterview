"""预算占用：花了多少、剩多少、余额能换成什么。

用户报"预算"时说的是"我打算花这么多"，不是"别超过这个数"。只报一句
"剩余 ¥2594"等于把决定权丢了——余额要么变成明确的可选项，要么明确
告诉他"这趟本来就只需要这么多"。

纯函数：不查库、不调模型，价格都来自已选定的商品与行程。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.trip_gear.itinerary import lodging_cost_for, total_cost
from app.trip_gear.models import GearItem, Itinerary, TripFacts
from app.trip_gear.verify import gear_cost

# 余额占预算超过这个比例，就值得提示"预算没用满"。
UNDERUSE_RATIO = 0.4

# 露营三大件：换住宿后不必买，算"可省"。
CAMPING_GEAR = ("帐篷", "睡袋", "防潮垫")


@dataclass
class BudgetSnapshot:
    trip: float
    equipment: float
    total: float
    budget: Optional[float]
    remaining: Optional[float]
    unknown: List[str]

    @property
    def ratio_left(self) -> Optional[float]:
        if not self.budget or self.budget <= 0 or self.remaining is None:
            return None
        return round(self.remaining / self.budget, 4)

    @property
    def underused(self) -> bool:
        ratio = self.ratio_left
        return ratio is not None and ratio >= UNDERUSE_RATIO


def snapshot(
    facts: TripFacts,
    itinerary: Itinerary,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
) -> BudgetSnapshot:
    trip = total_cost(itinerary)
    equipment, unknown = gear_cost(gear, products)
    total = round(trip + equipment, 2)
    budget = float(facts.budget) if facts.budget is not None else None
    remaining = round(budget - total, 2) if budget is not None else None
    return BudgetSnapshot(
        trip=trip,
        equipment=equipment,
        total=total,
        budget=budget,
        remaining=remaining,
        unknown=unknown,
    )
