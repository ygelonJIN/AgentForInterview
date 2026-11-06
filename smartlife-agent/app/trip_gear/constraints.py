"""约束派生：把一句自然语言需求变成约束清单 + 整趟事实。

这里只做"能落到可检查对象上"的派生：
- 用户明说的：目的地、天数、人数、预算、住宿、交通、同行儿童
- 规则推出的：周末 -> 2 天、预算默认含装备、夜间温度 -> 睡袋温标
- 外部事实：天气（由调用方注入，失败时明确降级）

槽位解析统一走 ``app.nlu.slots``，与 ``SharedConstraints`` 同源。
不在这里调用模型。模型只负责生成行程，派生规则保持确定性、可测试。
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from app.nlu.slots import extract_slots
from app.services.task_models import SharedConstraints
from app.trip_gear.models import Constraint, TripFacts

_TRANSPORT_LABELS = {"flight": "飞机", "train": "高铁/火车", "drive": "自驾"}


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def derive_constraints(
    message: str,
    shared: Optional[SharedConstraints] = None,
    today=None,
) -> Tuple[TripFacts, List[Constraint]]:
    """返回 (facts, constraints)。约束只包含这次真正用到的条目。

    ``shared`` 只作兜底：调用方已经抽好的共享约束里有的值，槽位没抽到时补上，
    保证两条链路不会互相矛盾。
    """
    text = str(message or "")
    slots = extract_slots(text, today=today)

    destination = slots.destination or (shared.destination if shared else "") or ""
    days = slots.days if slots.days is not None else (shared.days if shared else None)
    days_source = slots.source("days") if slots.days is not None else "共享约束"
    if days is None and "周末" in text:
        days, days_source = 2, "规则推导（周末按 2 天）"
    days_capped = False
    if days is not None and days > 60:
        # "三个月"这类超出单次规划上限，按 60 天处理并在清单里说明
        days, days_capped = 60, True

    people = slots.people if slots.people is not None else (shared.people if shared else None)
    people_source = slots.source("people") if slots.people is not None else (
        "共享约束" if people is not None else "未指定"
    )

    budget = slots.budget if slots.budget is not None else (shared.budget if shared else None)
    budget_source = slots.source("budget") if slots.budget is not None else "共享约束"

    facts = TripFacts(
        destination=destination,
        days=days,
        people=people,
        budget=budget,
        gear_budget=slots.gear_budget,
        budget_scope=slots.budget_scope,
        start_date=slots.start_date,
        lodging=slots.lodging,
        transport=slots.transport,
        with_kids=slots.with_kids,
    )

    constraints: List[Constraint] = []
    if destination:
        constraints.append(Constraint(text=f"目的地：{destination}", source="用户原话", key="destination"))
    if days:
        constraints.append(Constraint(text=f"天数：{days} 天", source=days_source, key="days"))
    if people is not None:
        constraints.append(Constraint(text=f"人数：{people} 人", source=people_source, key="people"))
    else:
        # 人数没说：按 1 人算，但明确标出来，别让用户以为系统知道
        constraints.append(
            Constraint(
                text="人数：未指定（按 1 人计）",
                source="默认规则",
                hard=False,
                key="people",
            )
        )
    if budget is not None:
        constraints.append(Constraint(text=f"预算：{budget:g} 元", source=budget_source, key="budget"))
        constraints.append(
            Constraint(
                text=(
                    "预算口径：含装备"
                    if slots.budget_scope == "all_in"
                    else "预算口径：不含装备"
                ),
                source=slots.source("budget_scope"),
                key="budget_scope",
            )
        )
    if slots.gear_budget is not None:
        constraints.append(
            Constraint(
                text=f"装备预算：{slots.gear_budget:g} 元",
                source="用户原话",
                key="gear_budget",
            )
        )
    if slots.lodging:
        constraints.append(
            Constraint(
                text="住宿：露营" if slots.lodging == "camp" else "住宿：酒店/民宿",
                source="用户原话",
                key="lodging",
            )
        )
    if slots.transport:
        constraints.append(
            Constraint(
                text=f"交通：{_TRANSPORT_LABELS.get(slots.transport, slots.transport)}",
                source="用户原话",
                key="transport",
            )
        )
    if slots.with_kids:
        constraints.append(Constraint(text="同行：有儿童", source="用户原话", key="with_kids"))
    if days_capped:
        constraints.append(
            Constraint(
                text="天数：原话超出单次规划上限，按 60 天处理",
                source="规则推导",
                hard=False,
            )
        )

    return facts, constraints


def apply_assisted(
    facts: TripFacts,
    constraints: List[Constraint],
    extra: dict,
) -> Tuple[TripFacts, List[Constraint]]:
    """把"模型补全"的槽位并进事实。

    只填空缺，不覆盖规则结果；来源统一标"模型补全"，用户能在约束清单里
    看出这一条是推断的，而不是自己说的。
    """
    if not extra:
        return facts, constraints

    merged = facts.model_copy(deep=True)
    existing_keys = {item.key for item in constraints if item.key}
    added: List[Tuple[str, str]] = []

    destination = str(extra.get("destination") or "").strip()
    if destination and not merged.destination and "destination" not in existing_keys:
        merged.destination = destination
        added.append((f"目的地：{destination}", "destination"))

    days = extra.get("days")
    if days and merged.days is None and "days" not in existing_keys:
        merged.days = int(days)
        added.append((f"天数：{merged.days} 天", "days"))

    people = extra.get("people")
    if people and merged.people is None:
        merged.people = int(people)
        # 之前那条"未指定（按 1 人计）"占位约束已经过时，先撤掉
        constraints = [
            item for item in constraints
            if not (item.key == "people" and "未指定" in item.text)
        ]
        added.append((f"人数：{merged.people} 人", "people"))

    budget = extra.get("budget")
    if budget and merged.budget is None and "budget" not in existing_keys:
        merged.budget = float(budget)
        added.append((f"预算：{merged.budget:g} 元", "budget"))

    start_date = str(extra.get("start_date") or "").strip()
    if start_date and not merged.start_date and "start_date" not in existing_keys:
        merged.start_date = start_date
        added.append((f"出发日期：{start_date}", "start_date"))

    for text, key in added:
        constraints.append(Constraint(text=text, source="模型补全", hard=True, key=key))
    return merged, constraints


_OWNED_DUPLICATE_HINTS = (
    "已有", "现有", "自带", "自备", "优先使用", "优先用", "不用买", "不需要买", "记得带",
)


def drop_owned_duplicates(
    constraints: List[Constraint],
    owned_names: Iterable[str],
) -> List[Constraint]:
    """丢掉"把用户已有的 X 纳入考虑"这类模型推断约束。

    这类条目在 owned 判定之后就是重复信息：东西已经进清单并标成"已有"了，
    再挂一条"模型推断 / 软 / 未校验"只会让用户以为系统没认出来。
    """
    names = [str(name).strip() for name in owned_names if len(str(name).strip()) >= 2]
    if not names:
        return constraints
    kept: List[Constraint] = []
    for item in constraints:
        if (
            item.source == "模型推断"
            and any(name in item.text for name in names)
            and any(hint in item.text for hint in _OWNED_DUPLICATE_HINTS)
        ):
            continue
        kept.append(item)
    return kept


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
