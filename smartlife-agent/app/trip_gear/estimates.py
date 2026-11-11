"""机票、城际交通与住宿的估算。

**不是报价，是估算。** 没有航班/酒店供应商数据，不能让"5000 元"只按地面
花费判——那会把机票和住宿当成 0，最后"预算还剩 1176"是假的。这里用一组
写死的、会展示给用户的单价，把两块补进 ``total_cost``；面板和 Markdown
必须标注"估算"和单价，不能让用户以为查到了实时价格。

单价改这里一处；``tests/test_trip_gear_estimates.py`` 钉住公式。
"""
from __future__ import annotations

import math
from typing import List, Tuple

from app.trip_gear.models import Itinerary, TripFacts

# 国内经济舱，按人、按段（单程）。上海—广东常见区间，不区分航司/舱位。
FLIGHT_PER_PERSON_LEG = 800.0
# 高铁/动车，按人、按段。省内城际（汕头—广州、广州—深圳）的保守均价。
RAIL_PER_PERSON_LEG = 120.0
# 经济型酒店，按间、按晚。默认两人一间。
HOTEL_PER_ROOM_NIGHT = 300.0
# 露营，按人、按晚（与 itinerary.LODGING_PRICE["camp"] 同口径）。
CAMP_PER_PERSON_NIGHT = 50.0


def _cities(facts: TripFacts) -> List[str]:
    cities = [str(city) for city in (facts.cities or []) if city]
    if not cities and facts.destination:
        cities = [facts.destination]
    return cities


def intercity_estimate(facts: TripFacts) -> Tuple[float, List[str]]:
    """往返大交通 + 城市之间的小交通。

    机票段数固定为 2（去、回）；城市间默认走高铁。没有出发地/目的地、
    或交通方式没指定时不猜，返回 0 并写明"未估算"。
    """
    if not facts.origin or not facts.destination:
        return 0.0, []
    cities = _cities(facts)
    between = max(0, len(cities) - 1)
    transport = str(facts.transport or "")

    if transport == "flight":
        flight_legs = 2
        rail_legs = between
        amount = (
            FLIGHT_PER_PERSON_LEG * flight_legs * facts.effective_people
            + RAIL_PER_PERSON_LEG * rail_legs * facts.effective_people
        )
        parts = [f"机票 {flight_legs} 段 × {facts.effective_people} 人 × ¥{FLIGHT_PER_PERSON_LEG:g}"]
        if rail_legs:
            parts.append(
                f"城际高铁 {rail_legs} 段 × {facts.effective_people} 人 × ¥{RAIL_PER_PERSON_LEG:g}"
            )
    elif transport == "train":
        rail_legs = 2 + between
        amount = RAIL_PER_PERSON_LEG * rail_legs * facts.effective_people
        parts = [f"高铁 {rail_legs} 段 × {facts.effective_people} 人 × ¥{RAIL_PER_PERSON_LEG:g}"]
    elif transport == "drive":
        return 0.0, ["自驾：油费/过路费未估算"]
    else:
        return 0.0, ["交通方式未指定：城际交通未估算"]

    note = f"城际交通估算：{' + '.join(parts)} ≈ ¥{amount:g}"
    return round(amount, 2), [note]


def lodging_estimate(facts: TripFacts, nights: int) -> Tuple[float, List[str]]:
    """按晚估算住宿；两人一间，用户给了每晚预算就按用户的。"""
    nights = max(0, int(nights or 0))
    if nights <= 0:
        return 0.0, []
    people = facts.effective_people
    if facts.lodging == "camp":
        amount = CAMP_PER_PERSON_NIGHT * people * nights
        return round(amount, 2), [
            f"住宿估算：露营 ¥{CAMP_PER_PERSON_NIGHT:g}/人/晚 × {people} 人 × {nights} 晚 ≈ ¥{amount:g}"
        ]

    rooms = max(1, math.ceil(people / 2))
    nightly = float(facts.lodging_budget or HOTEL_PER_ROOM_NIGHT)
    amount = nightly * rooms * nights
    basis = "用户每晚预算" if facts.lodging_budget else "经济型默认"
    return round(amount, 2), [
        f"住宿估算：¥{nightly:g}/间/晚 × {rooms} 间 × {nights} 晚 ≈ ¥{amount:g}（{basis}）"
    ]


def apply_estimates(itinerary: Itinerary, facts: TripFacts) -> Itinerary:
    """把住宿与城际估算写进行程；住宿只算 nights 晚，最后一天返程不收费。

    模型/兜底原来给每天（包括最后一天）都记一晚住宿，7 天行程会多算一晚；
    这里统一按 ``days - 1`` 晚重新分摊，避免"面板 7 晚、实际 6 晚"。
    """
    days = list(itinerary.days)
    nights = max(0, len(days) - 1)
    lodging, lodging_notes = lodging_estimate(facts, nights)
    if nights and days:
        per_night = round(lodging / nights, 2)
        for day in days[:-1]:
            day.lodging_cost = per_night
        days[-1].lodging_cost = 0.0
    else:
        for day in days:
            day.lodging_cost = 0.0

    intercity, intercity_notes = intercity_estimate(facts)
    itinerary.intercity_cost = intercity
    itinerary.estimate_notes = lodging_notes + intercity_notes
    return itinerary
