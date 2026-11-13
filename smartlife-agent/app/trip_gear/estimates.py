"""机票、城际交通与住宿的估算。

**不是报价，是估算。** 没有航班/酒店供应商数据，不能让"5000 元"只按地面
花费判——那会把机票和住宿当成 0，最后"预算还剩 1176"是假的。这里用一组
写死的、会展示给用户的单价，把两块补进 ``total_cost``；面板和 Markdown
必须标注"估算"和单价，不能让用户以为查到了实时价格。

两块的**输入**都来自追问卡，不是默认值：出发地/交通方式决定大交通怎么算，
住宿类型 + 每晚预算决定住宿怎么算。缺了就返回 0 并在 notes 里写明"未计入"，
让面板上的预算缺口是可见的，而不是被一个默认价悄悄填平。

单价改这里一处；``tests/test_trip_gear_estimates.py`` 钉住公式。
"""
from __future__ import annotations

import math
from typing import Any, List, Tuple

from app.trip_gear.models import Itinerary, TripFacts

# 国内经济舱，按人、按段（单程）。上海—广东常见区间，不区分航司/舱位。
FLIGHT_PER_PERSON_LEG = 800.0
# 高铁/动车，按人、按段。省内城际（汕头—广州、广州—深圳）的保守均价。
RAIL_PER_PERSON_LEG = 120.0
# 酒店/民宿参考单价，按间、按晚，两人一间。
#
# 这几个数**只用于两件事**：追问卡里的选项文案，以及用户明确选了某一档之后的
# 估算。它们不再是"没给就用 300"的默认值——住宿没问出来就不估算，并在面板里
# 写"未计入总预算"。拿默认价替用户决定，是这套系统里最容易被当成"乱算钱"的事。
HOTEL_PER_ROOM_NIGHT = 300.0          # 经济型
HOTEL_COMFORT_PER_ROOM_NIGHT = 500.0  # 舒适型
HOTEL_HIGH_PER_ROOM_NIGHT = 800.0     # 高档
HOMESTAY_PER_ROOM_NIGHT = 400.0       # 民宿（按间）
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
    if facts.lodging_budget is None:
        # 没说每晚多少就不估算。拿 300 默认填上去，用户看到的"剩余预算"
        # 是按一个没人同意的价格算出来的——宁可写"未计入"，也不编数字。
        return 0.0, [
            f"住宿未计入总预算：没说每晚预算（{nights} 晚 × {rooms} 间）"
        ]
    nightly = float(facts.lodging_budget)
    amount = nightly * rooms * nights
    return round(amount, 2), [
        f"住宿估算：¥{nightly:g}/间/晚 × {rooms} 间 × {nights} 晚 ≈ ¥{amount:g}（你的每晚预算）"
    ]


_TRANSPORT_ITEM_HINTS = (
    "高铁", "动车", "火车", "飞机", "航班", "机票", "自驾", "城际", "返程", "去程",
    "往返", "机场", "车站",
)


def _transport_items(days: List[Any]) -> List[Any]:
    """模型写进行程条目的大交通（"机票 上海→广州（2 人）"这种）。"""
    return [
        item
        for day in days
        for item in (day.items or [])
        if any(hint in str(item.activity) for hint in _TRANSPORT_ITEM_HINTS)
    ]


def _has_transport_items(days: List[Any]) -> bool:
    return bool(_transport_items(days))


def apply_estimates(
    itinerary: Itinerary,
    facts: TripFacts,
    *,
    trust_items: bool = False,
) -> Itinerary:
    """把住宿与城际估算写进行程；住宿只算 nights 晚，最后一天返程不收费。

    模型/兜底原来给每天（包括最后一天）都记一晚住宿，7 天行程会多算一晚；
    这里统一按 ``days - 1`` 晚重新分摊，避免"面板 7 晚、实际 6 晚"。

    ``trust_items=True`` 表示这份行程是模型排的：模型自己估的票价/住宿优先。
    - 模型把大交通写成了当天条目（"高铁 上海→杭州 ¥480"）时，不再叠一层总额
      估算，否则同一笔钱算两遍、预算凭空少一截；
    - 模型给了 ``lodging_cost`` 就按它的，否则按用户说的每晚预算分摊。
    """
    days = list(itinerary.days)
    nights = max(0, len(days) - 1)

    model_lodging = trust_items and any(float(day.lodging_cost or 0) > 0 for day in days)
    if model_lodging:
        lodging_notes = ["住宿按每日住宿行计入（模型估算）"]
    else:
        lodging, lodging_notes = lodging_estimate(facts, nights)
        if nights and days:
            per_night = round(lodging / nights, 2)
            for day in days[:-1]:
                day.lodging_cost = per_night
            days[-1].lodging_cost = 0.0
        else:
            for day in days:
                day.lodging_cost = 0.0

    transport_items = _transport_items(days) if trust_items else []
    itemized = sum(float(item.cost or 0) for item in transport_items)
    if transport_items and itemized > 0:
        # 模型已经在逐日条目里写了票价，这里只记账不重复加钱
        intercity, intercity_notes = 0.0, ["大交通按每日行程条目计入（模型估算）"]
    else:
        # 没写条目，或者写了条目却没写钱（cost=0）：按披露单价补，否则
        # 大交通会整块消失，预算看起来还剩一大堆。
        intercity, intercity_notes = intercity_estimate(facts)
        if transport_items:
            intercity_notes = intercity_notes + [
                "大交通条目没有金额，已按公开单价估算补上"
            ]
    itinerary.intercity_cost = intercity
    itinerary.estimate_notes = lodging_notes + intercity_notes
    return itinerary
