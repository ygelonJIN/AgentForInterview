"""独立硬约束检查器：只认场景事实与方案数据，不调用被测的判定实现。

存在的理由
----------
"硬约束违反数为 0"这个指标，如果由被测代码自己数，就等于自己给自己打分。
本模块从 ``facts`` / ``itinerary`` / ``gear`` / ``products`` 重新推一遍
"这份方案必须满足什么"，再逐条比对，所以：

- **不 import** ``verify`` / ``verdict`` / ``spec_match`` / ``repair`` / ``tradeoff``，
  连 ``itinerary.total_cost`` 也不复用（金额口径自己算一遍，口径不一致
  本身就是缺陷，应该被测出来）；
- 只用 ``models`` 里的数据结构，因为那是场景与方案共同的语言。

口径（与论文第五章一致）
------------------------
- **一条硬约束算一次**：同一类违反只计 1 条（"第 2、3 天都超支"仍是 1 条
  ``budget``），因为指标问的是"违反了几条约束"，不是"错了几处"；
- **未知不算违反**：规格未标注单列为 ``undecidable``，它是"必须披露"
  的义务，不是"已被证伪"的事实。把未知当违反会让"不判死"变成口号，
  把未知当满足则正是本项目要消灭的问题——两者都不能进 ``violations``；
- **不可行场景不追求 0**：场景本身不可行时，正确输出的违反数必然 > 0，
  要的是 ``verdict == infeasible`` 且披露与事实一一对上（见 ``compare``）。

覆盖的约束家族（``CODES``）
---------------------------
``destination`` / ``days`` / ``budget`` / ``gear_budget`` / ``lodging`` /
``camping_gear`` / ``sleeping_bag_temp`` / ``out_of_stock`` / ``lead_time`` /
``time_overlap`` / ``day_overload``。

其余约束（例如 ``people``）没有确定性判定对象，不在这里凭空造一个。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.trip_gear.models import GearItem, Itinerary, TripFacts

# 覆盖的约束家族。评测的披露一致性只在这个词表内对账，词表外的披露
# 单列为"无法核验"，不参与一致率，避免分母里混进别的模块的问题。
CODES = (
    "destination",
    "days",
    "budget",
    "gear_budget",
    "lodging",
    "camping_gear",
    "sleeping_bag_temp",
    "spec_insufficient",
    "rain",
    "out_of_stock",
    "lead_time",
    "time_overlap",
    "day_overload",
)

# 露营必备。与 ``verify`` 的清单一致；这里刻意再写一遍，不做引用：
# 两边同时被改错才叫"巧合"，一边改错会被评测抓住。
CAMPING_REQUIRED = ("帐篷", "睡袋", "防潮垫")

# 夜间温度留的余量（℃）。这是产品规则（产品口径见 spec_match），
# 检查器按"事实 - 余量"独立算一遍要求值，不调用那份实现。
TEMP_MARGIN_C = 5.0

# 防潮垫保温要求（R 值）：夜间 ≤5℃ 按 2.0，其余按 1.0。
R_VALUE_COLD = 2.0
R_VALUE_MILD = 1.0
COLD_THRESHOLD_C = 5.0

# 降水概率到这个数就要求带雨具。
RAIN_REQUIRED_P = 40

# 单日活动总量上限（分钟），与 itinerary.DAY_WINDOW_MIN 同一口径。
DAY_WINDOW_MIN = 840

_CAMP_MARKERS = ("露营", "帐篷", "营位", "扎营", "camp")
_HOTEL_MARKERS = ("酒店", "民宿", "青旅", "宾馆", "客栈", "hotel")
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True)
class HardViolation:
    """一条被认定违反的硬约束。``code`` 与 Issue.code 同一套词表。"""

    code: str
    label: str
    actual: Any = None
    limit: Any = None


@dataclass
class HardAudit:
    """一次审计的结果。

    ``undecidable`` 是"本可以违反、但当前无法判断"的项：规格未标注、
    采购时效未知。它们不是违反，但必须出现在披露清单里。
    """

    violations: List[HardViolation] = field(default_factory=list)
    undecidable: List[str] = field(default_factory=list)

    @property
    def codes(self) -> List[str]:
        return [item.code for item in self.violations]

    @property
    def count(self) -> int:
        return len(self.violations)


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _specs_of(gear: GearItem, product: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """规格来源：已有看自身，待购看商品；两者都有时以自身为准。

    商品行里的 specs 可能是 JSON 字符串（SQLite 取出来的原样），先解析。
    """
    specs = dict(gear.specs or {})
    raw = (product or {}).get("specs")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if isinstance(raw, dict):
        for key, value in raw.items():
            specs.setdefault(key, value)
    return specs


def trip_total(itinerary: Itinerary) -> float:
    """行程总花费 = 条目 + 住宿 + 城际。独立实现，不复用 itinerary.total_cost。"""
    items = sum(float(item.cost or 0) for day in itinerary.days for item in day.items)
    lodging = sum(float(day.lodging_cost or 0) for day in itinerary.days)
    intercity = float(getattr(itinerary, "intercity_cost", 0) or 0)
    return round(items + lodging + intercity, 2)


def gear_total(gear: Iterable[GearItem], products: Dict[str, Dict[str, Any]]) -> float:
    """待购装备花费；没有商品行的装备不计价（那是"未匹配"问题，不是钱）。"""
    total = 0.0
    for item in gear:
        if item.status != "need_buy":
            continue
        product = products.get(item.name) or {}
        total += float(product.get("price") or 0) * max(1, int(item.qty or 1))
    return round(total, 2)


def _camp_nights(itinerary: Itinerary) -> int:
    """当晚算不算露营：先看 lodging 字段，没填再看条目里有没有露营活动。"""
    nights = 0
    for day in itinerary.days:
        text = (day.lodging or "").strip().lower()
        if any(marker in text for marker in _CAMP_MARKERS):
            nights += 1
            continue
        activities = " ".join(item.activity or "" for item in day.items)
        if not text and any(marker in activities for marker in ("露营", "扎营")):
            nights += 1
    return nights


def _known_names(gear: Iterable[GearItem]) -> set:
    return {item.name for item in gear if item.status != "skipped"}


def _has_gear(names: Iterable[str], required: str) -> bool:
    """清单里有没有这件东西：允许自由命名（"双人帐篷"算帐篷）。

    精确相等会把"双人帐篷""-5℃ 羽绒睡袋"判成缺装备——模型和商品名
    都带修饰词，精确匹配等于要求它们一字不差。
    """
    for name in names:
        text = str(name or "")
        if required in text:
            return True
        if len(text) >= 2 and text in required:
            return True
    return False


def _days_left(facts: TripFacts, today: date) -> Optional[int]:
    if not facts.start_date:
        return None
    try:
        start = date.fromisoformat(str(facts.start_date)[:10])
    except ValueError:
        return None
    return (start - today).days


def _overlaps(itinerary: Itinerary) -> List[HardViolation]:
    found: List[HardViolation] = []
    for day in itinerary.days:
        spans = []
        for item in day.items:
            match = _TIME_RE.match(str(item.time or ""))
            if not match or not item.duration_min:
                continue
            start = int(match.group(1)) * 60 + int(match.group(2))
            spans.append((start, start + int(item.duration_min), item.activity))
        spans.sort()
        for previous, current in zip(spans, spans[1:]):
            if current[0] < previous[1]:
                found.append(
                    HardViolation(
                        code="time_overlap",
                        label=f"第{day.day}天时间冲突",
                        actual=f"{previous[2]} 与 {current[2]}",
                        limit="互不重叠",
                    )
                )
                break  # 同一天只计一条
    return found


def audit(
    facts: TripFacts,
    itinerary: Itinerary,
    gear: Sequence[GearItem] = (),
    products: Optional[Dict[str, Dict[str, Any]]] = None,
    today: Optional[Any] = None,
) -> HardAudit:
    """独立核对一份方案，返回硬约束违反与"不可判定"项。

    每个家族最多产生一条 ``HardViolation``：指标统计的是"违反了几条约束"。
    """
    products = products or {}
    today = today or date.today()
    if not isinstance(today, date):
        today = date.fromisoformat(str(today)[:10])

    audit_result = HardAudit()
    gear = list(gear)

    # 1. 目的地、天数
    if facts.destination and itinerary.destination and itinerary.destination != facts.destination:
        audit_result.violations.append(
            HardViolation("destination", "目的地不一致", itinerary.destination, facts.destination)
        )
    if facts.days and len(itinerary.days) != int(facts.days):
        audit_result.violations.append(
            HardViolation("days", "行程天数与要求不一致", len(itinerary.days), facts.days)
        )

    # 2. 预算（行程端 / 装备端 / 总计）
    trip = trip_total(itinerary)
    equipment = gear_total(gear, products)
    if facts.gear_budget is not None and equipment > float(facts.gear_budget) + 1e-6:
        audit_result.violations.append(
            HardViolation("gear_budget", "装备花费超出装备预算", equipment, facts.gear_budget)
        )
    if facts.budget is not None:
        chargeable = trip if facts.budget_scope == "trip_only" else round(trip + equipment, 2)
        if chargeable > float(facts.budget) + 1e-6:
            audit_result.violations.append(
                HardViolation("budget", "总花费超出预算", chargeable, facts.budget)
            )

    # 3. 住宿与露营装备
    camp_nights = _camp_nights(itinerary)
    if facts.lodging == "camp" and camp_nights == 0:
        audit_result.violations.append(
            HardViolation("lodging", "要求露营但行程没有露营", 0, "至少一晚")
        )
    if facts.lodging == "hotel" and camp_nights > 0:
        audit_result.violations.append(
            HardViolation("lodging", "要求住酒店却安排了露营", camp_nights, "0 晚")
        )
    if facts.is_camping:
        names = _known_names(gear)
        missing = [name for name in CAMPING_REQUIRED if not _has_gear(names, name)]
        if missing:
            audit_result.violations.append(
                HardViolation("camping_gear", "露营必备装备缺失", "、".join(missing), "帐篷、睡袋、防潮垫")
            )

    # 4. 睡袋温标：不够 = 违反；未标注 = 不可判定（必须披露，但不算违反）
    if facts.is_camping and facts.min_temp_c is not None:
        required = float(facts.min_temp_c) - TEMP_MARGIN_C
        bag = next((item for item in gear if item.name == "睡袋"), None)
        # 没睡袋已经由 camping_gear 计过一次，这里不重复计数
        if bag is not None:
            specs = _specs_of(bag, products.get("睡袋"))
            comfort = _number(specs.get("comfort_temp_c"))
            if comfort is None:
                audit_result.undecidable.append("sleeping_bag_temp")
            elif comfort > required + 1e-6:
                audit_result.violations.append(
                    HardViolation(
                        "sleeping_bag_temp",
                        "睡袋温标不够",
                        f"{comfort:g}℃",
                        f"≤{required:g}℃",
                    )
                )

    # 4.5 其余规格：帐篷容量、防潮垫保温值
    if facts.is_camping:
        tent = next((item for item in gear if item.name == "帐篷"), None)
        if tent is not None and facts.people is not None:
            capacity = _number(_specs_of(tent, products.get("帐篷")).get("capacity_persons"))
            if capacity is None:
                audit_result.undecidable.append("spec_unknown")
            elif capacity < float(facts.effective_people) - 1e-6:
                audit_result.violations.append(
                    HardViolation(
                        "spec_insufficient",
                        "帐篷容量不够",
                        f"{capacity:g} 人",
                        f"≥{facts.effective_people} 人",
                    )
                )
        pad = next((item for item in gear if item.name == "防潮垫"), None)
        if pad is not None and facts.min_temp_c is not None:
            r_value = _number(_specs_of(pad, products.get("防潮垫")).get("r_value"))
            required_r = R_VALUE_COLD if float(facts.min_temp_c) <= COLD_THRESHOLD_C else R_VALUE_MILD
            if r_value is None:
                audit_result.undecidable.append("spec_unknown")
            elif r_value < required_r - 1e-6:
                audit_result.violations.append(
                    HardViolation("spec_insufficient", "防潮垫保温值不够", f"R={r_value:g}", f"R≥{required_r:g}")
                )

    # 4.6 降水概率高却没有雨具
    if facts.rain_probability is not None and float(facts.rain_probability) >= RAIN_REQUIRED_P:
        names = _known_names(gear)
        if not any("雨" in name for name in names):
            audit_result.violations.append(
                HardViolation(
                    "rain",
                    "降水概率高但没有雨具",
                    f"{float(facts.rain_probability):g}%",
                    "应备雨具",
                )
            )

    # 5. 采购可履约：有货、来得及
    days_left = _days_left(facts, today)
    for item in gear:
        if item.status != "need_buy":
            continue
        product = products.get(item.name) or {}
        if not product:
            continue
        stock = _number(product.get("stock"))
        if stock is not None and stock <= 0:
            audit_result.violations.append(
                HardViolation("out_of_stock", f"{item.name} 无货", product.get("name") or item.name, "有货")
            )
            continue
        lead = _number(product.get("lead_time_days"))
        if lead is None or days_left is None:
            continue
        if lead > days_left:
            audit_result.violations.append(
                HardViolation(
                    "lead_time",
                    f"{item.name} 到货赶不上出发",
                    f"{lead:g} 天",
                    f"距出发 {days_left} 天",
                )
            )

    # 6. 行程自身结构
    audit_result.violations.extend(_overlaps(itinerary))
    for day in itinerary.days:
        minutes = sum(max(0, int(item.duration_min or 0)) for item in day.items)
        if minutes > DAY_WINDOW_MIN:
            audit_result.violations.append(
                HardViolation("day_overload", f"第{day.day}天安排过满", f"{minutes} 分钟", f"{DAY_WINDOW_MIN} 分钟")
            )
            break  # 同上：只计一条

    # 去重但保序：同一 code 只保留第一条（家族最多一条，防御未来改动）
    seen = set()
    unique: List[HardViolation] = []
    for violation in audit_result.violations:
        if violation.code in seen:
            continue
        seen.add(violation.code)
        unique.append(violation)
    audit_result.violations = unique
    audit_result.undecidable = list(dict.fromkeys(audit_result.undecidable))
    return audit_result


def compare(
    actual: Iterable[str],
    declared: Iterable[str],
) -> Dict[str, List[str]]:
    """实际违反 vs 已披露：漏披露、虚报违反。

    只比 ``CODES`` 里的家族。词表外的披露（别的模块的问题）单列，
    不参与一致率——否则分母会随无关改动漂移。
    """
    actual_set = {code for code in actual if code in CODES}
    declared_set = {code for code in declared if code in CODES}
    outside = sorted({code for code in declared if code and code not in CODES})
    return {
        "missed": sorted(actual_set - declared_set),
        "false_alarm": sorted(declared_set - actual_set),
        "unchecked": outside,
    }
