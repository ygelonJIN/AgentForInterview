"""把结构化结果渲染成用户可读的正文。"""
from __future__ import annotations

from typing import Any, Dict, List

from app.trip_gear.itinerary import total_cost
from app.trip_gear.models import Constraint, GearItem, Issue, Itinerary, TripFacts
from app.trip_gear.verify import gear_cost


def _money(value: float) -> str:
    return f"¥{float(value or 0):g}"


def _gear_line(item: GearItem, products: Dict[str, Dict[str, Any]]) -> str:
    head = f"- {item.name} ×{item.qty}"
    if item.reason:
        head += f"（{item.reason}）"
    product = products.get(item.name)
    if item.status == "need_buy" and product:
        head += (
            f" → {product.get('brand', '')} {product.get('name', '')}"
            f" {_money(product.get('price') or 0)}"
        )
    return head


def render(
    facts: TripFacts,
    constraints: List[Constraint],
    itinerary: Itinerary,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
    issues: List[Issue],
) -> str:
    lines: List[str] = []

    lines.append("## 约束清单")
    for constraint in constraints[:10]:
        marker = "（未满足）" if constraint.status == "violated" else ""
        lines.append(f"- {constraint.text}（{constraint.source}）{marker}")

    if itinerary.days:
        lines.append("")
        lines.append(f"## 行程（{itinerary.destination or facts.destination} · {len(itinerary.days)} 天）")
        for day in itinerary.days:
            lines.append(f"### 第 {day.day} 天")
            if not day.items:
                lines.append("- 暂无安排")
                continue
            for item in day.items:
                parts = [item.time, item.activity] if item.time else [item.activity]
                text = " · ".join(part for part in parts if part)
                if item.duration_min:
                    text += f" · {item.duration_min} 分钟"
                if item.cost:
                    text += f" · {_money(item.cost)}"
                lines.append(f"- {text}")

    owned = [item for item in gear if item.status == "owned"]
    need = [item for item in gear if item.status == "need_buy"]
    self_provided = [item for item in gear if item.status == "self_provided"]
    skipped = [item for item in gear if item.status == "skipped"]

    lines.append("")
    lines.append("## 装备清单")
    if owned:
        lines.append("**已有**")
        lines.extend(_gear_line(item, products) for item in owned)
    if need:
        lines.append("**待购**")
        lines.extend(_gear_line(item, products) for item in need)
    if self_provided:
        names = "、".join(item.name for item in self_provided)
        lines.append(f"**自备**：{names}")
    if skipped:
        names = "、".join(item.name for item in skipped)
        lines.append(f"**已按预算省略**：{names}")

    trip = total_cost(itinerary)
    equipment, unknown = gear_cost(gear, products)
    equipment_label = _money(equipment)
    if unknown:
        equipment_label += f"（{len(unknown)} 项未匹配，未计入）"
    total = round(trip + equipment, 2)
    lines.append("")
    lines.append("## 预算与校验")
    if facts.budget is None:
        lines.append(f"行程 {_money(trip)} + 装备 {equipment_label} = {_money(total)}（未设置预算）")
    elif facts.budget_scope == "trip_only":
        lines.append(
            f"行程 {_money(trip)}（预算 {_money(facts.budget)}，不含装备）"
            f" + 装备 {equipment_label} = {_money(total)}"
        )
        if facts.gear_budget is not None:
            lines.append(f"装备单独预算 {_money(facts.gear_budget)}")
    else:
        remaining = float(facts.budget) - total
        if remaining >= 0:
            tail = f"剩余 {_money(remaining)}"
        else:
            tail = f"超出 {_money(-remaining)}"
        lines.append(
            f"行程 {_money(trip)} + 装备 {equipment_label} = {_money(total)}"
            f" / 预算 {_money(facts.budget)}（含装备），{tail}"
        )

    if issues:
        lines.append("")
        lines.append("## 未满足的约束")
        for issue in issues:
            detail = issue.hint or ""
            if issue.actual is not None and issue.limit is not None:
                detail = f"实际 {issue.actual}，上限 {issue.limit}。{detail}"
            lines.append(f"- {issue.constraint}：{detail}".rstrip("："))
    else:
        lines.append("未发现硬约束冲突。")

    lines.append("")
    lines.append("行程费用为估算，商品价格来自平台数据库。")
    return "\n".join(lines)
