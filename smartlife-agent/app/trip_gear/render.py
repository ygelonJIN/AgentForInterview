"""把结构化结果渲染成用户可读的正文。"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from app.trip_gear.budget import snapshot as budget_snapshot
from app.trip_gear.itinerary import total_cost
from app.trip_gear.models import Constraint, GearItem, Issue, Itinerary, TripFacts
from app.trip_gear.verify import gear_cost


def _money(value: float) -> str:
    return f"¥{float(value or 0):g}"


def remaining_ratio_text(snap) -> str:
    ratio = snap.ratio_left
    return f"{ratio:.0%}" if ratio is not None else "一部分"


def _defaults_note(facts: TripFacts) -> str:
    """用了默认值就说出来：和面板尾部的提醒同口径。"""
    bits = []
    if facts.people is None:
        bits.append("人数按 1 人计")
    if not facts.start_date:
        bits.append(f"出发日期按今天（{date.today():%Y-%m-%d}）计")
    if not bits:
        return ""
    return f"默认值：{'、'.join(bits)}。如果和实际不符，告诉我，我重排。"


def _gear_line(item: GearItem, products: Dict[str, Dict[str, Any]]) -> str:
    """一行装备。

    买到的东西只显示「为什么带 → 买哪件」，不再显示规则内部的装备名
    （"保暖层""防水鞋"是系统说法，用户看商品名就够了）；没买到的才需要
    显示装备名，否则用户不知道缺什么。
    """
    product = products.get(item.name) if item.status == "need_buy" else None

    if product:
        brand = str(product.get("brand") or "")
        name = str(product.get("name") or "")
        # 商品名里已经带品牌时别再前缀一次，否则会出现"黑鹿 黑鹿帐篷 双人"。
        label = name if (brand and brand in name) else f"{brand} {name}".strip()
        reason = f"（{item.reason}）" if item.reason else ""
        qty = f" ×{item.qty}" if item.qty > 1 else ""
        head = f"- {reason} → {label} {_money(product.get('price') or 0)}{qty}"
        match = product.get("match") or {}
        if match.get("by_preference"):
            head += "（按你偏好的品牌）"
        if match.get("kind") == "substitute":
            query = match.get("query") or ""
            head += f"（库里没有同名商品，这是按「{query}」检索到的同类款）"
        return head

    head = f"- {item.name} ×{item.qty}"
    if item.reason:
        head += f"（{item.reason}）"
    return head


def render(
    facts: TripFacts,
    constraints: List[Constraint],
    itinerary: Itinerary,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
    issues: List[Issue],
    ambiguous_owned: Optional[List[str]] = None,
    spend: Optional[Dict[str, Any]] = None,
) -> str:
    lines: List[str] = []

    # 只留"内容 + 来源"：硬/软与校验状态是系统内部口径，展示在 05 里说结果就够了。
    lines.append("## 约束清单")
    for constraint in constraints[:12]:
        lines.append(f"- {constraint.text}（来源：{constraint.source}）")

    if itinerary.days:
        lines.append("")
        lines.append(f"## 行程（{itinerary.destination or facts.destination} · {len(itinerary.days)} 天）")
        for day in itinerary.days:
            city = f"（{day.city}）" if getattr(day, "city", "") else ""
            lines.append(f"### 第 {day.day} 天{city}")
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

    # 清单里的每条都必须对应一个真实商品；库里没有的已经在 shop 阶段被移除了。
    owned = [item for item in gear if item.status == "owned"]
    need = [item for item in gear if item.status == "need_buy"]
    skipped = [item for item in gear if item.status == "skipped"]

    lines.append("")
    lines.append("## 装备清单")
    if owned:
        lines.append("**已有**")
        lines.extend(_gear_line(item, products) for item in owned)
    if need:
        lines.append("**待购**")
        lines.extend(_gear_line(item, products) for item in need)
    if skipped:
        names = "、".join(item.name for item in skipped)
        lines.append(f"**已按预算省略**：{names}")
    budget_blocked = [item for item in need if "预算不足" in (item.reason or "")]
    if budget_blocked:
        lines.append("**预算不足，未选款**：" + "、".join(item.name for item in budget_blocked))

    trip = total_cost(itinerary)
    equipment, _unknown = gear_cost(gear, products)
    equipment_label = _money(equipment)
    total = round(trip + equipment, 2)
    if ambiguous_owned:
        lines.append("")
        lines.append("## 待确认")
        lines.append(
            "你提到过这些，但没说是哪些具体物品，我先没算成已有，"
            "也没重复计费。告诉我具体是什么，我按已有处理："
        )
        for phrase in ambiguous_owned:
            lines.append(f"- 「{phrase}」")

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
        snap = budget_snapshot(facts, itinerary, gear, products)
        remaining = snap.remaining or 0.0
        if remaining >= 0:
            ratio = snap.ratio_left
            tail = f"剩余 {_money(remaining)}"
            if ratio is not None:
                tail += f"（预算的 {ratio:.0%}）"
        else:
            tail = f"超出 {_money(-remaining)}"
        lines.append(
            f"行程 {_money(trip)} + 装备 {equipment_label} = {_money(total)}"
            f" / 预算 {_money(facts.budget)}（含装备），{tail}"
        )
        upgrades = list((spend or {}).get("upgrades") or [])
        if upgrades:
            lines.append(f"为用满预算做了 {len(upgrades)} 处升级：")
            for upgrade in upgrades:
                # 商品名自身已经说明是什么，不再前缀内部装备名。
                lines.append(
                    f"- {upgrade['from']} {_money(upgrade['from_price'])} → "
                    f"{upgrade['to']} {_money(upgrade['to_price'])}"
                    f"（多花 {_money(upgrade['delta'])}）"
                )
        elif snap.underused:
            reason = (spend or {}).get("reason") or "没有可升级的款"
            lines.append(
                f"预算还剩 {remaining_ratio_text(snap)}（{_money(remaining)}），"
                f"但{reason}。"
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
        lines.append("未发现约束冲突。")

    note = _defaults_note(facts)
    if note:
        lines.append("")
        lines.append(note)

    lines.append("")
    lines.append("行程费用为估算，商品价格来自平台数据库。")
    return "\n".join(lines)
