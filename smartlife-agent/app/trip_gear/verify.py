"""校验：预算（含装备）、时间、天数、装备齐全性。

只做确定性检查；模型审核不参与这里的判定。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.trip_gear.itinerary import total_cost, validate as validate_itinerary
from app.trip_gear.models import Constraint, GearItem, Issue, Itinerary, TripFacts


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


def verify(
    facts: TripFacts,
    constraints: List[Constraint],
    itinerary: Itinerary,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
) -> List[Issue]:
    for constraint in constraints:
        constraint.status = "ok"

    issues: List[Issue] = []
    trip = total_cost(itinerary)
    equipment, unknown = gear_cost(gear, products)
    total = round(trip + equipment, 2)

    if facts.budget is not None:
        if facts.budget_scope == "trip_only":
            if trip > facts.budget:
                issues.append(
                    Issue(
                        constraint="预算（行程）",
                        actual=trip,
                        limit=facts.budget,
                        hint="行程费用超出预算，减少收费项目或降低住宿标准",
                    )
                )
        elif total > facts.budget:
            issues.append(
                Issue(
                    constraint="预算（含装备）",
                    actual=total,
                    limit=facts.budget,
                    hint="优先削减可选装备，或减少收费项目",
                )
            )

    if facts.gear_budget is not None and equipment > facts.gear_budget:
        issues.append(
            Issue(
                constraint="装备预算",
                actual=equipment,
                limit=facts.gear_budget,
                hint="装备花费超出单独预算，换更便宜的型号",
            )
        )

    for name in unknown:
        issues.append(
            Issue(
                constraint=f"装备：{name}",
                hint="商品库暂未匹配到商品，未计入预算",
                severity="minor",
            )
        )

    if facts.days and len(itinerary.days) != int(facts.days):
        issues.append(
            Issue(
                constraint="天数",
                actual=len(itinerary.days),
                limit=facts.days,
                hint="生成的行程天数与要求不一致",
            )
        )

    issues.extend(validate_itinerary(itinerary))

    if facts.is_camping:
        names = {item.name for item in gear if item.status != "skipped"}
        for required in ("帐篷", "睡袋", "防潮垫"):
            if required not in names:
                issues.append(
                    Issue(
                        constraint=f"露营装备：{required}",
                        hint="露营过夜必须有这一项",
                    )
                )

    budget_violated = any(
        issue.constraint.startswith(("预算", "装备预算")) and issue.severity == "critical"
        for issue in issues
    )
    if budget_violated:
        for constraint in constraints:
            if "预算" in constraint.text:
                constraint.status = "violated"

    return issues
