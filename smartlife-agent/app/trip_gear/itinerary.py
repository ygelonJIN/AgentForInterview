"""逐日行程：渲染、时间校验和模型不可用时的兜底。"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

from app.trip_gear.models import DayPlan, FixedEvent, Issue, Itinerary, ItineraryItem, TripFacts

DAY_WINDOW_MIN = 840  # 8:00-22:00

# 住宿估算价（元/人/晚）。没有供应商数据，只能给保守估价；凡是展示到
# 用户面前的数字都要带"估算"，不能当成报价。
LODGING_PRICE = {"camp": 50.0, "hotel": 400.0}


def lodging_cost_for(lodging: str, people=None, nights: int = 1, nightly=None) -> float:
    """按住宿类型算一晚的人均费用；认不出的类型按 0（当作不需要花钱）。

    ``people`` 允许为 None（用户没说人数），此时按 1 人计。
    ``nightly`` 是用户明说的"一晚 300 左右"——有它就用它，不要拿默认的
    酒店价去算，否则用户说 300、系统按 400 记，最后莫名超预算。
    """
    price = float(nightly) if nightly else LODGING_PRICE.get(str(lodging or "").strip().lower(), 0.0)
    count = max(1, int(people or 1))
    return round(price * count * max(1, int(nights)), 2)

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def activities_text(itinerary: Itinerary) -> str:
    return " ".join(
        item.activity
        for day in itinerary.days
        for item in day.items
        if item.activity
    )


def total_cost(itinerary: Itinerary) -> float:
    """行程总费用 = 条目花费 + 住宿费。

    住宿单列一项而不是摊进条目里：换住宿时要能单独算出"这一项变了多少"。
    """
    items = sum(float(item.cost or 0) for day in itinerary.days for item in day.items)
    lodging = sum(float(day.lodging_cost or 0) for day in itinerary.days)
    return round(items + lodging, 2)


def _parse_time(value: str) -> Optional[int]:
    match = _TIME_RE.match(str(value or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def validate(itinerary: Itinerary) -> List[Issue]:
    """只做确定性检查：单日活动总量、时间重叠、空日程。"""
    issues: List[Issue] = []
    for day in itinerary.days:
        if not day.items:
            issues.append(
                Issue(
                    constraint=f"第 {day.day} 天日程",
                    hint="当天没有任何安排",
                    severity="minor",
                )
            )
            continue
        total_minutes = sum(max(0, int(item.duration_min or 0)) for item in day.items)
        if total_minutes > DAY_WINDOW_MIN:
            issues.append(
                Issue(
                    constraint=f"第 {day.day} 天时间",
                    actual=f"{total_minutes} 分钟",
                    limit=f"{DAY_WINDOW_MIN} 分钟",
                    hint="单日活动排得太满，需要删减或调整时长",
                )
            )
        timed = []
        for item in day.items:
            start = _parse_time(item.time)
            if start is None or not item.duration_min:
                continue
            timed.append((start, start + int(item.duration_min), item.activity))
        timed.sort(key=lambda row: row[0])
        for previous, current in zip(timed, timed[1:]):
            if current[0] < previous[1]:
                issues.append(
                    Issue(
                        constraint=f"第 {day.day} 天时间冲突",
                        actual=f"{previous[2]} 与 {current[2]} 重叠",
                        hint="相邻安排时间重叠，需要顺延或删减",
                    )
                )
    return issues


def _sort_key(item: ItineraryItem) -> int:
    start = _parse_time(item.time)
    return start if start is not None else 24 * 60


def assign_cities(days: int, cities: Sequence[str]) -> Dict[int, str]:
    """把城市按顺序均分到各天，余数给前面的城市（7 天 3 城 → 3/2/2）。

    这是规则不是建议：天气要按天落到具体城市，所以先定一个确定的分法，
    模型和兜底都照它排；用户改主意就改城市顺序。
    """
    ordered = [str(city) for city in cities if city]
    total = max(1, int(days or 1))
    if not ordered:
        return {}
    base, extra = divmod(total, len(ordered))
    result: Dict[int, str] = {}
    day = 1
    for index, city in enumerate(ordered):
        span = base + (1 if index < extra else 0)
        for _ in range(span):
            if day > total:
                break
            result[day] = city
            day += 1
    return result


def _appointment_day(event: FixedEvent, day_numbers: List[int]) -> int:
    """约见没定哪天时排中间那天：第一天多在赶路，最后一天要返程。"""
    if event.day and event.day in day_numbers:
        return event.day
    if not day_numbers:
        return 0
    return day_numbers[len(day_numbers) // 2]


def apply_fixed_events(itinerary: Itinerary, facts: TripFacts) -> Itinerary:
    """把固定活动钉进对应那天。

    - ``booked`` 只处理写明了第几天的：没说是哪天就不猜——猜一天等于替用户
      决定哪天去。
    - ``appointment``（约好见人）必须排进去，哪天没定就排中间那天；它锁的是
      "这趟要留出这半天"，不是"必须在这一天"。
    - ``planned`` 只是"想去"，排不进去也不该挤掉别的安排。

    钉进去后当天若超时，**让路的是可调整的条目，不是固定活动**——固定活动是
    外部约束，删它等于替用户退票。
    """
    by_day = {day.day: day for day in itinerary.days}
    day_numbers = sorted(by_day)
    events = [
        (event, event.day)
        for event in (facts.fixed_events or [])
        if event.status == "booked" and event.day
    ]
    events += [
        (event, _appointment_day(event, day_numbers))
        for event in (facts.fixed_events or [])
        if event.status == "appointment"
    ]
    for event, day_number in events:
        plan = by_day.get(day_number)
        if plan is None:
            continue
        # 模型已经排了这件事，别再排一遍
        if any(
            event.title in item.activity or (event.venue and event.venue in item.activity)
            for item in plan.items
        ):
            continue
        booked = event.status == "booked"
        pinned = ItineraryItem(
            time=event.start or ("19:30" if booked else "14:00"),
            activity=(
                f"{event.title}" if booked else f"{event.title}（半天，地点固定）"
            ) + (f"（{event.venue}）" if event.venue else ""),
            duration_min=max(30, int(event.duration_min or 120)),
            cost=float(event.cost or 0),
        )
        # 那天原本安排了返程/离开，而已订活动在它之后——说明当晚不走，
        # 删掉返程而不是把活动挪走：已订活动是用户已经买票的外部约束。
        # 约见不享受这条：见完朋友照样可以走。
        if booked:
            for item in list(plan.items):
                if any(word in item.activity for word in ("返程", "回程", "离开")) and _sort_key(item) <= _sort_key(pinned):
                    plan.items.remove(item)
        else:
            # 约见占的是半天：和它撞时段的安排删掉，否则同一天 14:00 排两件事。
            start = _sort_key(pinned)
            end = start + max(30, pinned.duration_min)
            for item in list(plan.items):
                other_start = _sort_key(item)
                other_end = other_start + max(0, int(item.duration_min or 0))
                if other_start < end and start < other_end:
                    plan.items.remove(item)

        plan.items.append(pinned)
        plan.items.sort(key=_sort_key)

        total = sum(max(0, int(item.duration_min or 0)) for item in plan.items)
        while total > DAY_WINDOW_MIN and len(plan.items) > 1:
            removable = [item for item in plan.items if item is not pinned]
            if not removable:
                break
            plan.items.remove(removable[-1])
            total = sum(max(0, int(item.duration_min or 0)) for item in plan.items)
    return itinerary


def fallback_itinerary(facts: TripFacts) -> Itinerary:
    """没有模型时也能跑通链路；费用只是保守估算。"""
    days = max(1, int(facts.days or 2))
    place = facts.destination or "目的地"
    city_of_day = assign_cities(days, facts.cities)
    plans: List[DayPlan] = []
    for day in range(1, days + 1):
        here = city_of_day.get(day, place)
        if day == 1:
            items = [
                ItineraryItem(time="10:00", activity=f"到达{here}并安顿", duration_min=120, cost=0),
                ItineraryItem(time="12:00", activity="午餐", duration_min=60, cost=60),
                ItineraryItem(time="14:00", activity=f"{here}周边活动", duration_min=180, cost=100),
                ItineraryItem(time="18:00", activity="晚餐", duration_min=90, cost=80),
            ]
        elif day == days:
            items = [
                ItineraryItem(time="08:30", activity="早餐", duration_min=45, cost=30),
                ItineraryItem(time="09:30", activity=f"{here}上午活动", duration_min=150, cost=80),
                ItineraryItem(time="12:00", activity="午餐", duration_min=60, cost=60),
                ItineraryItem(time="13:30", activity="返程", duration_min=120, cost=0),
            ]
        else:
            items = [
                ItineraryItem(time="08:30", activity="早餐", duration_min=45, cost=30),
                ItineraryItem(time="09:30", activity=f"{here}上午活动", duration_min=180, cost=100),
                ItineraryItem(time="12:30", activity="午餐", duration_min=60, cost=60),
                ItineraryItem(time="14:00", activity=f"{here}下午活动", duration_min=180, cost=100),
                ItineraryItem(time="18:00", activity="晚餐", duration_min=90, cost=80),
            ]
        previous = city_of_day.get(day - 1, "")
        if day > 1 and previous and previous != here:
            # 换城那天上午在路上：早餐和上午安排都让位
            items = [
                item for item in items
                if item.activity != "早餐" and "上午活动" not in item.activity
            ]
            items.insert(0, ItineraryItem(
                time="08:30",
                activity=f"从{previous}前往{here}（城际交通）",
                duration_min=120,
                cost=100,
            ))
        plans.append(
            DayPlan(
                day=day,
                city=city_of_day.get(day, ""),
                items=items,
                lodging=facts.lodging,
                lodging_cost=lodging_cost_for(
                    facts.lodging, facts.effective_people, nightly=facts.lodging_budget
                ),
            )
        )
    return apply_fixed_events(Itinerary(destination=place, days=plans), facts)
