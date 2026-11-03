"""校验：逐条约束核对 + 时间、天数、装备齐全性。

只做确定性检查；模型审核不参与这里的判定。

约束按 ``Constraint.key`` 派发到具体检查器，检查结果回写到
``constraint.status``：ok（满足）/ violated（违反）/ relaxed（软约束违反）/
unverified（该项无法自动判定，需人工确认）。只有 violated 才产生 Issue，
避免"未校验"把问题列表淹没。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.trip_gear.itinerary import total_cost, validate as validate_itinerary
from app.trip_gear.models import Constraint, GearItem, Issue, Itinerary, TripFacts

# 能自动判定的约束键；其余键只展示，标 unverified。
_CHECKABLE = {
    "destination",
    "days",
    "budget",
    "gear_budget",
    "lodging",
    "with_kids",
    "sleeping_bag_temp",
    "rain",
    "weather",
}

# 有信息价值但当前无法确定性判定的约束键，明确标注为未校验。
_NOT_AUTO = {"people", "transport"}

# 仅作口径说明，不做判定。
_INFORMATIONAL = {"budget_scope"}

# 住宿字段可能是枚举值（camp/hotel），也可能是自由文本，两种都要认。
_CAMP_MARKERS = ("露营", "帐篷", "营位", "扎营", "camp")
_HOTEL_MARKERS = ("酒店", "民宿", "青旅", "宾馆", "客栈", "hotel")


def gear_cost(gear: List[GearItem], products: Dict[str, Dict[str, Any]]) -> Tuple[float, List[str]]:
    """返回 (已计价装备总价, 未匹配到商品的装备名)。"""
    known = 0.0
    unknown: List[str] = []
    for item in gear:
        if item.status != "need_buy":
            continue
        product = products.get(item.name)
        if not product:
            unknown.append(item.name)
            continue
        known += float(product.get("price") or 0) * item.qty
    return round(known, 2), unknown


def _violate(
    constraint: Constraint,
    issues: List[Issue],
    actual: Any = None,
    limit: Any = None,
    hint: str = "",
) -> None:
    """记录一次违反：软约束标 relaxed，硬约束产生 critical issue。"""
    if constraint.hard:
        constraint.status = "violated"
        issues.append(
            Issue(
                constraint=constraint.text,
                actual=actual,
                limit=limit,
                hint=hint or "该约束未被满足",
                severity="critical",
            )
        )
    else:
        constraint.status = "relaxed"
        issues.append(
            Issue(
                constraint=constraint.text,
                actual=actual,
                limit=limit,
                hint=hint or "软约束未满足，可接受",
                severity="minor",
            )
        )


def _camping_days(itinerary: Itinerary) -> int:
    """统计安排为露营的夜数；兼容枚举值与中文自由文本。"""
    count = 0
    for day in itinerary.days:
        text = (day.lodging or "").strip().lower()
        if any(marker in text for marker in _CAMP_MARKERS):
            count += 1
    return count


def _known_gear_names(gear: List[GearItem]) -> set:
    return {item.name for item in gear if item.status != "skipped"}


def check_constraints(
    facts: TripFacts,
    constraints: List[Constraint],
    itinerary: Itinerary,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
) -> List[Issue]:
    """按 key 逐条核对约束，回写 status 并返回违规 issue。"""
    issues: List[Issue] = []
    trip = total_cost(itinerary)
    equipment, _ = gear_cost(gear, products)
    total = round(trip + equipment, 2)
    names = _known_gear_names(gear)
    camp_days = _camping_days(itinerary)

    for constraint in constraints:
        key = constraint.key
        if not key:
            # 没有 key 的是纯展示条目（如自由文本补充），不参与自动判定。
            continue

        if key == "destination":
            if facts.destination and itinerary.destination and itinerary.destination != facts.destination:
                _violate(
                    constraint, issues,
                    actual=itinerary.destination, limit=facts.destination,
                    hint="行程目的地与要求不一致",
                )
        elif key == "days":
            if facts.days and len(itinerary.days) != int(facts.days):
                _violate(
                    constraint, issues,
                    actual=len(itinerary.days), limit=facts.days,
                    hint="生成的行程天数与要求不一致",
                )
        elif key == "budget":
            if facts.budget is None:
                continue
            chargeable = trip if facts.budget_scope == "trip_only" else total
            if chargeable > float(facts.budget) + 1e-6:
                _violate(
                    constraint, issues,
                    actual=chargeable, limit=facts.budget,
                    hint=(
                        "行程费用超出预算，减少收费项目或降低住宿标准"
                        if facts.budget_scope == "trip_only"
                        else "含装备总花费超出预算，优先削减可选装备"
                    ),
                )
        elif key == "gear_budget":
            if facts.gear_budget is not None and equipment > float(facts.gear_budget) + 1e-6:
                _violate(
                    constraint, issues,
                    actual=equipment, limit=facts.gear_budget,
                    hint="装备花费超出单独预算，换更便宜的型号",
                )
        elif key == "lodging":
            if facts.lodging == "camp" and camp_days == 0:
                _violate(
                    constraint, issues,
                    actual="行程未安排露营住宿", limit="至少一晚露营",
                    hint="露营约束没有被行程兑现",
                )
            elif facts.lodging == "hotel" and camp_days > 0:
                _violate(
                    constraint, issues,
                    actual=f"{camp_days} 晚露营", limit="不住露营",
                    hint="住宿偏好为酒店/民宿，行程不应安排露营",
                )
        elif key == "with_kids":
            if not any(name.startswith("儿童") for name in names):
                _violate(
                    constraint, issues,
                    actual="装备清单无儿童项", limit="至少一项儿童装备",
                    hint="同行有儿童，装备清单缺少儿童用品",
                )
        elif key == "sleeping_bag_temp":
            bag = next((item for item in gear if item.name == "睡袋"), None)
            if bag is None:
                _violate(
                    constraint, issues,
                    actual="装备清单无睡袋", limit=constraint.text,
                    hint="夜间温度要求温标，但清单没有睡袋",
                )
            elif "温标" not in (bag.reason or ""):
                issues.append(
                    Issue(
                        constraint=constraint.text,
                        actual=bag.reason or "未标注温标",
                        limit=constraint.text,
                        hint="睡袋未标注温标，需人工确认",
                        severity="minor",
                    )
                )
        elif key == "rain":
            if facts.rain_probability is not None and facts.rain_probability >= 40 and "雨具" not in names:
                _violate(
                    constraint, issues,
                    actual=f"降水概率 {facts.rain_probability:g}%", limit="应备雨具",
                    hint="降水概率较高，装备清单缺少雨具",
                )
        elif key in _NOT_AUTO:
            constraint.status = "unverified"
        elif key in _INFORMATIONAL:
            continue
        elif key not in _CHECKABLE:
            constraint.status = "unverified"

    return issues


def verify(
    facts: TripFacts,
    constraints: List[Constraint],
    itinerary: Itinerary,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
) -> List[Issue]:
    for constraint in constraints:
        constraint.status = "ok"

    # 1. 逐条约束核对
    issues = check_constraints(facts, constraints, itinerary, gear, products)

    # 2. 商品库未覆盖的装备如实披露，不编价格
    _, unknown = gear_cost(gear, products)
    for name in unknown:
        issues.append(
            Issue(
                constraint=f"装备：{name}",
                hint="商品库暂未匹配到商品，未计入预算",
                severity="minor",
            )
        )

    # 3. 行程自身的时间与结构校验
    issues.extend(validate_itinerary(itinerary))

    # 4. 没有 days 约束时仍兜底核对天数
    if not any(item.key == "days" for item in constraints) and facts.days:
        if len(itinerary.days) != int(facts.days):
            issues.append(
                Issue(
                    constraint="天数",
                    actual=len(itinerary.days),
                    limit=facts.days,
                    hint="生成的行程天数与要求不一致",
                )
            )

    # 5. 露营必备装备
    if facts.is_camping:
        names = _known_gear_names(gear)
        for required in ("帐篷", "睡袋", "防潮垫"):
            if required not in names:
                issues.append(
                    Issue(
                        constraint=f"露营装备：{required}",
                        hint="露营过夜必须有这一项",
                    )
                )

    return issues
