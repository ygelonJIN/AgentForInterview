"""逐日行程：渲染、时间校验和模型不可用时的兜底。"""
from __future__ import annotations

import re
from typing import List, Optional

from app.trip_gear.models import DayPlan, Issue, Itinerary, ItineraryItem, TripFacts

DAY_WINDOW_MIN = 840  # 8:00-22:00

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def activities_text(itinerary: Itinerary) -> str:
    return " ".join(
        item.activity
        for day in itinerary.days
        for item in day.items
        if item.activity
    )


def total_cost(itinerary: Itinerary) -> float:
    return round(
        sum(float(item.cost or 0) for day in itinerary.days for item in day.items),
        2,
    )


def item_count(itinerary: Itinerary) -> int:
    return sum(len(day.items) for day in itinerary.days)


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


def fallback_itinerary(facts: TripFacts) -> Itinerary:
    """没有模型时也能跑通链路；费用只是保守估算。"""
    days = max(1, int(facts.days or 2))
    place = facts.destination or "目的地"
    plans: List[DayPlan] = []
    for day in range(1, days + 1):
        if day == 1:
            items = [
                ItineraryItem(time="10:00", activity=f"到达{place}并安顿", duration_min=120, cost=0),
                ItineraryItem(time="12:00", activity="午餐", duration_min=60, cost=60),
                ItineraryItem(time="14:00", activity=f"{place}周边活动", duration_min=180, cost=100),
                ItineraryItem(time="18:00", activity="晚餐", duration_min=90, cost=80),
            ]
        elif day == days:
            items = [
                ItineraryItem(time="08:30", activity="早餐", duration_min=45, cost=30),
                ItineraryItem(time="09:30", activity=f"{place}上午活动", duration_min=150, cost=80),
                ItineraryItem(time="12:00", activity="午餐", duration_min=60, cost=60),
                ItineraryItem(time="13:30", activity="返程", duration_min=120, cost=0),
            ]
        else:
            items = [
                ItineraryItem(time="08:30", activity="早餐", duration_min=45, cost=30),
                ItineraryItem(time="09:30", activity=f"{place}上午活动", duration_min=180, cost=100),
                ItineraryItem(time="12:30", activity="午餐", duration_min=60, cost=60),
                ItineraryItem(time="14:00", activity=f"{place}下午活动", duration_min=180, cost=100),
                ItineraryItem(time="18:00", activity="晚餐", duration_min=90, cost=80),
            ]
        plans.append(DayPlan(day=day, items=items, lodging=facts.lodging))
    return Itinerary(destination=place, days=plans)
