"""校验：逐条约束核对 + 时间、天数、装备齐全性。

只做确定性检查；模型审核不参与这里的判定。

约束按 ``Constraint.key`` 派发到具体检查器，检查结果回写到
``constraint.status``：ok（满足）/ violated（违反）/ relaxed（软约束违反）/
unverified（该项无法自动判定，需人工确认）。只有 violated 才产生 Issue，
避免"未校验"把问题列表淹没。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.trip_gear.itinerary import total_cost, validate as validate_itinerary
from app.trip_gear.models import Constraint, GearItem, Issue, Itinerary, TripFacts
from app.trip_gear import purchase
from app.trip_gear.spec_match import (
    STATUS_INSUFFICIENT,
    STATUS_UNKNOWN,
    SpecRequirement,
    check as check_spec,
    evaluate as evaluate_specs,
    resolve_specs,
    spec_requirements,
)

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

# 仅作口径说明，不做判定。出发日期只用来算到货时效，本身没有"对不对"。
_INFORMATIONAL = {"budget_scope", "start_date"}

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


def _bag_requirement(facts: TripFacts, constraint: Constraint) -> Optional[SpecRequirement]:
    """睡袋温标要求：优先按事实算，事实缺失时回落到约束文本里的数字。"""
    requirements = spec_requirements(facts, {"睡袋"})
    if requirements:
        return requirements[0]
    match = re.search(r"≤\s*(-?\d+(?:\.\d+)?)", constraint.text or "")
    if not match:
        return None
    return SpecRequirement(
        gear="睡袋",
        key="comfort_temp_c",
        op="<=",
        value=float(match.group(1)),
        reason=constraint.text,
    )


def _check_bag(
    constraint: Constraint,
    bag: GearItem,
    facts: TripFacts,
    products: Dict[str, Dict[str, Any]],
    issues: List[Issue],
) -> None:
    """核对睡袋温标是否真的够用。

    旧实现只查 ``reason`` 里有没有"温标"两个字，等于没校验：15 温标的
    夏季睡袋去 3℃ 的夜里照样判通过。这里改成真比数值。
    """
    requirement = _bag_requirement(facts, constraint)
    if requirement is None:
        constraint.status = "unverified"
        return

    result = check_spec(requirement, resolve_specs(bag, products.get("睡袋")))
    if result.status == STATUS_INSUFFICIENT:
        _violate(
            constraint, issues,
            actual=f"{result.actual:g}{requirement.unit}",
            limit=f"≤{requirement.value:g}{requirement.unit}",
            hint=f"{result.hint}；换更低温度标的睡袋，或租一件",
        )
    elif result.status == STATUS_UNKNOWN:
        constraint.status = "unverified"
        issues.append(
            Issue(
                constraint=constraint.text,
                actual="未标注温标",
                limit=constraint.text,
                hint="睡袋温标不明，无法判断够不够用，需确认",
                severity="minor",
                blocking_risk=True,
            )
        )


def _check_specs(
    facts: TripFacts,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
    constraints: List[Constraint],
) -> List[Issue]:
    """规格核对：够不够用。

    睡袋温标由 ``sleeping_bag_temp`` 约束分支负责，这里跳过，
    否则同一件事会在结果里报两遍。
    """
    handled = {item.key for item in constraints if item.key}
    requirements = [
        req for req in spec_requirements(facts, _known_gear_names(gear))
        if not (req.gear == "睡袋" and "sleeping_bag_temp" in handled)
    ]
    issues: List[Issue] = []
    for result in evaluate_specs(gear, products, requirements=requirements):
        req = result.requirement
        if result.status == STATUS_INSUFFICIENT:
            issues.append(
                Issue(
                    constraint=f"{req.gear}：{req.label}不够",
                    actual=f"{result.actual:g}{req.unit}",
                    limit=req.describe(),
                    hint=result.hint,
                    severity="critical",
                )
            )
        elif result.status == STATUS_UNKNOWN:
            issues.append(
                Issue(
                    constraint=f"{req.gear}：{req.label}未知",
                    actual="未标注",
                    limit=req.describe(),
                    hint=result.hint,
                    severity="minor",
                    # 规格一问就清楚，且答案可能直接判"不够用"，所以值得打断。
                    blocking_risk=True,
                )
            )
    return issues


def _describe_substitutes(subs: List[Any]) -> str:
    """把替代款拼成一句人话，+"多花"、-"省下"都写清楚。"""
    parts = []
    for sub in subs:
        delta = sub.delta_price
        sign = "+" if delta >= 0 else "-"
        parts.append(f"{sub.name}（{sub.lead_time_days} 天到，{sign}{abs(delta):.0f} 元）")
    return "、".join(parts)


def _check_purchases(
    facts: TripFacts,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
    today: Optional[Any] = None,
    find_alternatives: Optional[Any] = None,
) -> List[Issue]:
    """采购可履约：买得到、来得及；赶不上就给替代链，而不是只报错。

    与"库未覆盖"不重复：那条管"没搜到商品"，这条管"搜到了但买不到/赶不上"。
    """
    # 出发日期默认按今天，但那是占位不是事实：拿它逐件判"赶不上"会把整份
    # 方案判成不可行，还会把真正的冲突挤出面板。日期没定就整组不判，
    # 由约束清单和面板尾部的默认值提醒负责交代。
    if not facts.start_date:
        return []

    issues: List[Issue] = []
    for result in purchase.check_purchases(gear, products, facts, today=today):
        product = products.get(result.gear) or {}

        if result.status == purchase.STATUS_UNKNOWN:
            # 离出发还远时，缺时效几乎不会造成"赶不上"，不值得为它打断用户。
            risky = result.days_left is not None and result.days_left <= 7
            issues.append(
                Issue(
                    constraint=f"装备：{result.gear} 收货时效未定",
                    actual=result.product,
                    hint=result.hint,
                    severity="minor",
                    blocking_risk=risky,
                )
            )
            continue

        if result.status == purchase.STATUS_OK:
            continue

        substitutes = purchase.find_substitutes(
            product, result.days_left, find_alternatives
        )
        options = [sub.as_dict() for sub in substitutes]

        if result.status == purchase.STATUS_OUT_OF_STOCK:
            hint = result.hint
            hint += (
                f"；同品类可换：{_describe_substitutes(substitutes)}"
                if substitutes else "；同品类暂时没有可换的现货"
            )
            issues.append(
                Issue(
                    constraint=f"装备：{result.gear} 无货",
                    actual=result.product,
                    limit="需要有货",
                    hint=hint,
                    severity="critical",
                    alternatives=options,
                )
            )
            continue

        # 到货赶不上：优先给"能赶上的替代款"，其次给自提这条退路。
        hint = result.hint
        hint += (
            f"；可换：{_describe_substitutes(substitutes)}"
            if substitutes else "；同品类没有能赶在出发前到货的款"
        )
        if purchase.pickup_available(product):
            hint += "；或到店自提（该商品有货，假设同城）"
        issues.append(
            Issue(
                constraint=f"装备：{result.gear} 到货赶不上出发",
                actual=f"需 {result.lead_time_days} 天",
                limit=f"距出发 {result.days_left} 天",
                hint=hint,
                severity="critical",
                alternatives=options,
            )
        )
    return issues


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
            else:
                _check_bag(constraint, bag, facts, products, issues)
        elif key == "rain":
            if facts.rain_probability is not None and facts.rain_probability >= 40 and not any("雨" in name for name in names):
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
    today: Optional[Any] = None,
    find_alternatives: Optional[Any] = None,
) -> List[Issue]:
    """全面校验。

    ``today`` 只用于"到货赶不上"；``find_alternatives`` 是可选的检索回调，
    传了才会在赶不上时找替代款，否则只报问题。
    """
    for constraint in constraints:
        constraint.status = "ok"

    # 1. 逐条约束核对
    issues = check_constraints(facts, constraints, itinerary, gear, products)

    # 每晚住宿预算：内部按人均计价，这里也按人均比，避免"两人一间"被算成两倍
    if facts.lodging_budget is not None:
        over = [
            (day.day, float(day.lodging_cost or 0) / max(1, facts.effective_people))
            for day in itinerary.days
            if float(day.lodging_cost or 0) / max(1, facts.effective_people)
            > float(facts.lodging_budget) + 1e-6
        ]
        if over:
            day_number, per_person = over[0]
            issues.append(
                Issue(
                    constraint=(
                        f"住宿：第{day_number}天人均每晚 {per_person:.0f} 元"
                        f" 超过每晚预算 {facts.lodging_budget:g} 元"
                    ),
                    actual=round(per_person, 2),
                    limit=facts.lodging_budget,
                    hint="换更便宜的住处，或放宽每晚住宿预算",
                    # 没有可自动执行的改法，只披露不判死
                    severity="minor",
                )
            )

    # 2. 没选到商品的装备如实披露；但要分清"库里没有"和"预算不够"，
    #    否则用户会以为商品库缺货，实际是自己给的钱不够。
    _, unknown = gear_cost(gear, products)
    by_name = {item.name: item for item in gear}
    for name in unknown:
        reason = (by_name.get(name).reason if by_name.get(name) else "") or ""
        if "预算不足" in reason:
            issues.append(
                Issue(
                    constraint=f"装备：{name} 预算不足",
                    hint="当前预算内没有可选的款，未购买；放宽预算或减需求再试",
                    severity="minor",
                )
            )
        else:
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

    # 6. 规格核对：有这件东西不等于够用
    issues.extend(_check_specs(facts, gear, products, constraints))

    # 7. 采购可履约：买得到、来得及
    issues.extend(
        _check_purchases(
            facts, gear, products, today=today, find_alternatives=find_alternatives
        )
    )

    return issues
