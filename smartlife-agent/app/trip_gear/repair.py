"""超预算修复：分层候选与动作应用。

修复不再只看一张写死的小件白名单。每件待购装备都有两种可行动作：

- **降档**（downgrade）：同子品类里换更便宜的商品，功能不丢，优先采用。
- **省略**（skip）：只允许砍掉明确的可选项（``gear.OPTIONAL``），
  露营三大件、证件、药品、儿童用品这类不进入候选。

候选按"能省多少钱"降序排列，同额时降档优先，因为省略会丢功能。
真正不可动的必需品不会产生候选——修复轮次用尽后由校验如实披露，
而不是靠删掉装备把报表做平。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.trip_gear.gear import OPTIONAL
from app.trip_gear.itinerary import lodging_cost_for
from app.trip_gear.models import DayPlan, GearItem, Itinerary, TripFacts
from app.trip_gear.shopping import ALIASES, normalize_gear_name

# 即使预算超了也不能通过"省略"消失的装备：安全、合规与行程成立的前提。
PROTECTED_SKIP = {
    "身份证", "手机", "充电器", "充电宝", "常用药",
    "帐篷", "睡袋", "防潮垫", "雨具",
    "儿童衣物", "儿童常用药", "湿巾",
}

# 降档时至少要便宜这个比例，避免为了几毛钱反复换商品。
MIN_DOWNGRADE_RATIO = 0.15

FindAlternatives = Callable[..., List[dict]]


@dataclass
class RepairAction:
    """一次修复动作。"""

    kind: str                      # downgrade / skip / change_lodging / soften_activity
    name: str
    saving: float
    reason: str
    replacement: Optional[Dict[str, Any]] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    # 会不会改动用户明说过的条件（比如把"露营"改成住民宿）。为 True 的动作
    # 不能自动执行，只能作为选项摆给用户。
    requires_confirm: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "saving": round(self.saving, 2),
            "reason": self.reason,
            "replacement": self.replacement,
            "requires_confirm": self.requires_confirm,
        }


def _price(product: Optional[Dict[str, Any]]) -> float:
    return float((product or {}).get("price") or 0)


# 降档的品类词表：替代品名称必须命中其中之一，才算"同一类东西"。
# 只按子品类换会把帐篷换成卡式炉、把防水鞋换成登山杖，所以显式列出。
FAMILY_KEYWORDS = {
    "帐篷": ("帐篷",),
    "睡袋": ("睡袋",),
    "防潮垫": ("垫",),
    "保暖层": ("羽绒", "外套", "冲锋衣"),
    "防水鞋": ("鞋",),
    "登山鞋": ("鞋",),
    "跑步鞋": ("鞋",),
    "雨具": ("雨伞",),
    "雨伞": ("雨伞",),
    "帽子": ("帽",),
    "墨镜": ("墨镜", "眼镜"),
    "防晒霜": ("防晒",),
    "双肩包": ("背包",),
    "背包": ("背包",),
    "换洗衣物": ("T恤", "上衣", "衬衫"),
    "儿童衣物": ("T恤", "上衣", "衬衫"),
    "T恤": ("T恤",),
    "裙子": ("裙",),
    "裙装": ("裙",),
    "速干衣": ("速干",),
    "登山杖": ("登山杖",),
    "营地灯": ("灯",),
    "头灯": ("灯",),
    "洗衣袋": ("袋",),
    "防水袋": ("袋",),
    "液体分装瓶": ("瓶",),
}


def _family_match(item_name: str, candidate_name: str) -> bool:
    """替代品是否与装备属于同一类。

    模型补充的装备名常带修饰词（防水运动鞋、便携洗漱包），先规范化再查表；
    表里没有的一律不降档——宁可如实披露超支，也不换错东西。
    """
    keywords: tuple = ()
    for key in (item_name, normalize_gear_name(item_name)):
        if key in FAMILY_KEYWORDS:
            keywords = FAMILY_KEYWORDS[key]
            break
        alias = ALIASES.get(key)
        if alias and alias in FAMILY_KEYWORDS:
            keywords = FAMILY_KEYWORDS[alias]
            break
    if not keywords:
        return False
    name = str(candidate_name or "")
    return any(word in name for word in keywords)


def _can_skip(item: GearItem) -> bool:
    return item.name in OPTIONAL and item.name not in PROTECTED_SKIP


def _downgrade_for(
    item: GearItem,
    current: Dict[str, Any],
    find_alternatives: Optional[FindAlternatives],
) -> Optional[RepairAction]:
    """在同一子品类里找更便宜的替代品。"""
    if not find_alternatives:
        return None
    subcategory = str(current.get("subcategory") or "")
    if not subcategory:
        return None
    limit_price = _price(current) * (1 - MIN_DOWNGRADE_RATIO)
    try:
        rows = list(
            find_alternatives(
                subcategory=subcategory,
                max_price=limit_price,
                limit=10,
            )
            or []
        )
    except Exception:
        return None

    rows = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("name")
        and row.get("name") != current.get("name")
        and (row.get("stock") or 0) > 0
        and _price(row) < _price(current)
        and _family_match(item.name, str(row.get("name") or ""))
    ]
    if not rows:
        return None
    rows.sort(key=_price)
    best = rows[0]
    saving = (_price(current) - _price(best)) * item.qty
    if saving <= 0:
        return None
    return RepairAction(
        kind="downgrade",
        name=item.name,
        saving=saving,
        reason=(
            f"{item.name} 换同品类更便宜的 {best.get('brand', '')}{best.get('name', '')}，"
            f"省 {saving:.0f} 元"
        ).strip(),
        replacement=best,
    )


# 露营三大件：换住宿时它们才真正不必买，所以省下的钱要一次性算进来。
CAMPING_GEAR = ("帐篷", "睡袋", "防潮垫")

# 砍掉一个收费项目至少得省这么多钱才值得牺牲体验。
MIN_ACTIVITY_COST = 50.0


def _lodging_swap_action(
    facts: TripFacts,
    itinerary: Itinerary,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
) -> Optional[RepairAction]:
    """露营改住民宿：省下露营三大件，代价是住宿费变贵。

    这是唯一能"用装备换行程"的动作——不买帐篷睡袋防潮垫，改付住宿费。
    只有净省为正才提出来，否则等于花钱买罪受。它改了用户明说的住宿方式，
    所以必须用户点头，不能自动执行。
    """
    if not facts.is_camping:
        return None

    camping = [
        item for item in gear
        if item.name in CAMPING_GEAR and item.status == "need_buy"
    ]
    if not camping:
        return None

    gear_saving = sum(
        float((products.get(item.name) or {}).get("price") or 0) * item.qty
        for item in camping
    )
    nights = max(1, len(itinerary.days))
    old_lodging = sum(float(day.lodging_cost or 0) for day in itinerary.days)
    if old_lodging <= 0:
        old_lodging = lodging_cost_for("camp", facts.people, nights)
    new_lodging = lodging_cost_for("hotel", facts.people, nights)
    net = round(gear_saving - (new_lodging - old_lodging), 2)
    if net <= 0:
        return None

    return RepairAction(
        kind="change_lodging",
        name="露营改住民宿",
        saving=net,
        reason=(
            f"不买{'、'.join(item.name for item in camping)}省 {gear_saving:.0f} 元，"
            f"住宿多花 {new_lodging - old_lodging:.0f} 元，净省 {net:.0f} 元；"
            "但这改变了你原本的露营安排"
        ),
        requires_confirm=True,
        meta={
            "from": "camp",
            "to": "hotel",
            "nights": nights,
            "gear_saving": round(gear_saving, 2),
            "lodging_delta": round(new_lodging - old_lodging, 2),
        },
    )


def _soften_action(itinerary: Itinerary) -> Optional[RepairAction]:
    """砍掉最贵的一项收费活动：不花钱、不推翻住宿，只是少玩一个项目。"""
    candidates = [
        (day, item)
        for day in itinerary.days
        for item in day.items
        if float(item.cost or 0) >= MIN_ACTIVITY_COST
    ]
    if not candidates:
        return None
    day, item = max(candidates, key=lambda pair: float(pair[1].cost or 0))
    return RepairAction(
        kind="soften_activity",
        name=str(item.activity),
        saving=float(item.cost or 0),
        reason=f"预算紧张，去掉第 {day.day} 天的收费项目「{item.activity}」",
        meta={"day": day.day, "activity": item.activity},
    )


def repair_candidates(
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
    find_alternatives: Optional[FindAlternatives] = None,
    limit: int = 8,
    facts: Optional[TripFacts] = None,
    itinerary: Optional[Itinerary] = None,
) -> List[RepairAction]:
    """按可省金额降序给出修复候选。

    传了 ``facts`` 与 ``itinerary`` 才会给出"改行程"类动作（换住宿、砍收费项目）：
    没有行程就无从判断改它能省多少。
    """
    actions: List[RepairAction] = []
    for item in gear:
        if item.status != "need_buy":
            continue
        current = products.get(item.name)
        if current:
            downgrade = _downgrade_for(item, current, find_alternatives)
            if downgrade:
                actions.append(downgrade)
        if _can_skip(item):
            actions.append(
                RepairAction(
                    kind="skip",
                    name=item.name,
                    saving=_price(current) * item.qty,
                    reason=f"预算紧张，省略可选装备 {item.name}",
                )
            )

    if facts is not None and itinerary is not None:
        swap = _lodging_swap_action(facts, itinerary, gear, products)
        if swap:
            actions.append(swap)
    if itinerary is not None:
        soften = _soften_action(itinerary)
        if soften:
            actions.append(soften)

    actions.sort(key=lambda action: (action.saving, action.kind == "downgrade"), reverse=True)
    return actions[:limit]


def apply_action(
    action: RepairAction,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
    itinerary: Optional[Itinerary] = None,
    facts: Optional[TripFacts] = None,
    force: bool = False,
) -> None:
    """把候选动作落到数据上；原地修改 gear / products / itinerary。

    ``force=False`` 时拒绝执行需要用户确认的动作——改掉用户明说的条件
    不能悄悄发生，那和偷换需求没区别。
    """
    if action.requires_confirm and not force:
        raise PermissionError(f"{action.kind} 需要用户确认，不能自动执行")

    if action.kind == "change_lodging" and itinerary is not None:
        people = facts.people if facts else 1
        for day in itinerary.days:
            day.lodging = "hotel"
            day.lodging_cost = lodging_cost_for("hotel", people)
        # 三大件不必买了：标记为不需要，并从待购里移除，避免继续计价。
        for item in gear:
            if item.name in CAMPING_GEAR:
                item.status = "skipped"
                item.reason = (item.reason + "；改住民宿后不必购买").strip("；")
                products.pop(item.name, None)
        return

    if action.kind == "soften_activity" and itinerary is not None:
        target_day = (action.meta or {}).get("day")
        for day in itinerary.days:
            if target_day is not None and day.day != target_day:
                continue
            day.items = [item for item in day.items if item.activity != action.name]
            if target_day is not None:
                break
        return

    if action.kind == "downgrade" and action.replacement:
        products[action.name] = action.replacement
        for item in gear:
            if item.name == action.name:
                item.reason = (item.reason + "；已降档替换").strip("；")
        return

    for item in gear:
        if item.name == action.name:
            item.status = "skipped"
            break
    products.pop(action.name, None)
