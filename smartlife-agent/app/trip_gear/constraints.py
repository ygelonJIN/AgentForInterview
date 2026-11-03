"""约束派生：把一句自然语言需求变成约束清单 + 整趟事实。

这里只做“能落到可检查对象上”的派生：
- 用户明说的：目的地、天数、人数、预算、住宿、交通、同行儿童
- 规则推出的：周末 -> 2 天、预算默认含装备、夜间温度 -> 睡袋温标
- 外部事实：天气（由调用方注入，失败时明确降级）

不在这里调用模型。模型只负责生成行程，派生规则保持确定性、可测试。
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

from app.services.task_models import SharedConstraints, extract_shared_constraints
from app.trip_gear.models import Constraint, TripFacts

_CN_NUM = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

_DEST_RE = re.compile(
    r"(?:去|到|在|前往|想去|准备去)\s*([\u4e00-\u9fa5]{2,6}?)"
    r"(?=露营|玩|旅游|出差|度假|徒步|爬山|住|待|，|。|,|$)"
)

_KNOWN_PLACES = (
    "莫干山", "千岛湖", "安吉", "临安", "桐庐", "舟山", "黄山", "武功山",
    "杭州", "北京", "上海", "成都", "三亚", "西安", "丽江", "大理",
    "厦门", "桂林", "张家界", "云南", "新疆", "西藏",
)

_PEOPLE_RE = re.compile(r"([一两二三四五六七八九十\d]+)\s*(?:个?人|位|口)")

_EXCLUDE_SCOPE = (
    "不含装备", "装备另算", "装备另计", "装备不算", "装备除外",
    "装备单独算", "装备单算", "装备自己买",
)
_INCLUDE_SCOPE = ("含装备", "包含装备", "装备也算", "算上装备", "预算含装备")

_GEAR_BUDGET_RE = re.compile(
    r"装备(?:预算|费用|花费)\s*(?:为|是|:|：)?\s*(\d+(?:\.\d+)?)"
)
_GEAR_BUDGET_RE2 = re.compile(r"装备\s*(\d+(?:\.\d+)?)\s*(?:元|块)")

_CAMP_WORDS = ("露营", "营地", "扎营", "帐篷过夜")
_HOTEL_WORDS = ("酒店", "民宿", "青旅", "宾馆", "客栈")
_KID_WORDS = ("孩子", "小孩", "儿童", "宝宝", "亲子", "带娃")
_FLIGHT_WORDS = ("飞机", "航班", "机票")
_TRAIN_WORDS = ("高铁", "动车", "火车")
_DRIVE_WORDS = ("自驾", "开车", "租车")


def _destination(text: str) -> str:
    match = _DEST_RE.search(text or "")
    if match:
        return match.group(1).strip()
    return next((place for place in _KNOWN_PLACES if place in (text or "")), "")


def _people(text: str, shared: SharedConstraints) -> Tuple[int, str]:
    match = _PEOPLE_RE.search(text or "")
    if match:
        raw = match.group(1)
        if raw.isdigit():
            return int(raw), "用户原话"
        if raw in _CN_NUM:
            return _CN_NUM[raw], "用户原话"
    if shared.people and shared.people > 1:
        return shared.people, "用户原话"
    return 1, "默认规则"


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def derive_constraints(
    message: str,
    shared: Optional[SharedConstraints] = None,
) -> Tuple[TripFacts, List[Constraint]]:
    """返回 (facts, constraints)。约束只包含这次真正用到的条目。"""
    text = str(message or "")
    shared = shared or extract_shared_constraints(text)

    destination = _destination(text) or (shared.destination or "")
    days = shared.days
    days_source = "用户原话"
    if days is None and "周末" in text:
        days = 2
        days_source = "规则推导（周末按 2 天）"

    people, people_source = _people(text, shared)

    budget = shared.budget
    if any(word in text for word in _EXCLUDE_SCOPE):
        budget_scope = "trip_only"
        scope_source = "用户原话"
    elif any(word in text for word in _INCLUDE_SCOPE):
        budget_scope = "all_in"
        scope_source = "用户原话"
    else:
        budget_scope = "all_in"
        scope_source = "默认规则"

    gear_budget = None
    gear_budget_match = _GEAR_BUDGET_RE.search(text) or _GEAR_BUDGET_RE2.search(text)
    if gear_budget_match:
        gear_budget = _float(gear_budget_match.group(1))
        budget_scope = "trip_only"

    lodging = ""
    if any(word in text for word in _CAMP_WORDS):
        lodging = "camp"
    elif any(word in text for word in _HOTEL_WORDS):
        lodging = "hotel"

    transport = ""
    if any(word in text for word in _FLIGHT_WORDS):
        transport = "flight"
    elif any(word in text for word in _TRAIN_WORDS):
        transport = "train"
    elif any(word in text for word in _DRIVE_WORDS):
        transport = "drive"

    with_kids = any(word in text for word in _KID_WORDS)

    facts = TripFacts(
        destination=destination,
        days=days,
        people=people,
        budget=budget,
        gear_budget=gear_budget,
        budget_scope=budget_scope,
        start_date=shared.start_date.isoformat() if shared.start_date else None,
        lodging=lodging,
        transport=transport,
        with_kids=with_kids,
    )

    constraints: List[Constraint] = []
    if destination:
        constraints.append(Constraint(text=f"目的地：{destination}", source="用户原话", key="destination"))
    if days:
        constraints.append(Constraint(text=f"天数：{days} 天", source=days_source, key="days"))
    constraints.append(Constraint(text=f"人数：{people} 人", source=people_source, key="people"))
    if budget is not None:
        constraints.append(
            Constraint(text=f"预算：{budget:g} 元", source="用户原话", key="budget")
        )
        constraints.append(
            Constraint(
                text=(
                    "预算口径：含装备"
                    if budget_scope == "all_in"
                    else "预算口径：不含装备"
                ),
                source=scope_source,
                key="budget_scope",
            )
        )
    if gear_budget is not None:
        constraints.append(
            Constraint(text=f"装备预算：{gear_budget:g} 元", source="用户原话", key="gear_budget")
        )
    if lodging:
        constraints.append(
            Constraint(
                text="住宿：露营" if lodging == "camp" else "住宿：酒店/民宿",
                source="用户原话",
                key="lodging",
            )
        )
    if transport:
        labels = {"flight": "飞机", "train": "高铁/火车", "drive": "自驾"}
        constraints.append(
            Constraint(
                text=f"交通：{labels.get(transport, transport)}",
                source="用户原话",
                key="transport",
            )
        )
    if with_kids:
        constraints.append(Constraint(text="同行：有儿童", source="用户原话", key="with_kids"))

    return facts, constraints


def merge_inferred(
    constraints: List[Constraint],
    inferred: Iterable[dict],
) -> List[Constraint]:
    """把模型推断出的约束并进清单（来源标"模型推断"，默认软约束）。

    规则能判定的键不覆盖：规则比模型可靠，重复条目只保留规则那条。
    """
    # 只有这些键才代表"同一类约束"，不能重复；"other" 之类只按文本去重。
    identity_keys = {
        "destination", "days", "people", "budget", "budget_scope",
        "gear_budget", "lodging", "transport", "with_kids",
        "weather", "sleeping_bag_temp", "rain",
    }
    known_keys = {item.key for item in constraints if item.key in identity_keys}
    seen_texts = {item.text.strip() for item in constraints}
    for raw in inferred or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if len(text) < 2 or text in seen_texts:
            continue
        key = str(raw.get("key") or "").strip()
        # 模型常把 key 写成一句概述，只有命中已知身份键才有校验意义，其余归为 other。
        if key not in identity_keys:
            key = "other"
        if key in identity_keys and key in known_keys:
            continue
        constraints.append(
            Constraint(
                text=text,
                source="模型推断",
                hard=bool(raw.get("hard", False)),
                key=key,
            )
        )
        seen_texts.add(text)
        if key in identity_keys:
            known_keys.add(key)
    return constraints


def enrich_weather(
    facts: TripFacts,
    constraints: List[Constraint],
    weather: Optional[dict],
) -> List[Constraint]:
    """把天气事实并入约束清单；拿不到就明确降级，不猜温度。"""
    if not weather:
        constraints.append(
            Constraint(
                text="天气：未获取到可验证数据，按通用情况准备",
                source="降级",
                hard=False,
                status="unverified",
                key="weather",
            )
        )
        return constraints

    min_temp = _float(weather.get("temperature_min"))
    max_temp = _float(weather.get("temperature_max"))
    rain = _float(weather.get("precipitation_probability"))
    condition = str(weather.get("condition") or "").strip()

    parts = [part for part in (condition, _temp_text(min_temp, max_temp)) if part]
    summary = "，".join(parts) or "天气数据已获取"
    facts.min_temp_c = min_temp
    facts.rain_probability = rain
    facts.weather_summary = summary
    constraints.append(Constraint(text=f"天气：{summary}", source="天气工具", key="weather"))

    if facts.is_camping and min_temp is not None:
        constraints.append(
            Constraint(
                text=f"睡袋温标 ≤ {min_temp - 5:.0f}℃",
                source="规则推导（夜间温度留 5℃ 余量）",
                key="sleeping_bag_temp",
            )
        )
    if rain is not None and rain >= 40:
        constraints.append(
            Constraint(text=f"降水概率：{rain:g}%", source="天气工具", key="rain")
        )
    return constraints


def _temp_text(min_temp: Optional[float], max_temp: Optional[float]) -> str:
    if min_temp is not None and max_temp is not None:
        return f"{min_temp:.0f}~{max_temp:.0f}℃"
    if min_temp is not None:
        return f"最低 {min_temp:.0f}℃"
    if max_temp is not None:
        return f"最高 {max_temp:.0f}℃"
    return ""
