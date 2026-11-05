"""预算利用：把该花的钱花掉，而不是留一串"可以这样用"。

用户报预算说的是"我打算花这么多"。只给选项等于把决定权丢回去，所以这里
直接执行：在同品类里换更好的款，直到钱用得差不多或没有可换的。

四条边界，越界就是乱花钱：

1. **只换库里的东西**，价格来自商品行，不编；
2. **不超预算**，每次升级的差价必须落在剩余额度内；
3. **不降级保障**：规格要求（温标、容量）必须仍然满足，规格未知的候选直接跳过——
   拿"不知道"去换"确定够用"是赌博；
4. **不碰用户明说的条件**：不换住宿、不改行程，只动还没买的装备。

钱花不掉时如实说"库里没有更贵的可用款"，不硬凑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.trip_gear.models import GearItem, TripFacts
from app.trip_gear.purchase import days_until, departure_date
from app.trip_gear.spec_match import (
    STATUS_INSUFFICIENT,
    STATUS_UNKNOWN,
    as_specs,
    check as check_spec,
    spec_requirements,
)

FindAlternatives = Callable[..., List[dict]]

# 低于这个差价就不折腾了：换一次要多等几天到货，省不了几十块不值得。
MIN_UPGRADE_DELTA = 50.0


@dataclass
class Upgrade:
    gear: str
    from_name: str
    from_price: float
    to_name: str
    to_price: float
    qty: int = 1

    @property
    def delta(self) -> float:
        """这件装备升级一共多花多少（单价差 × 数量）。"""
        return round((self.to_price - self.from_price) * self.qty, 2)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "gear": self.gear,
            "from": self.from_name,
            "from_price": self.from_price,
            "to": self.to_name,
            "to_price": self.to_price,
            "qty": self.qty,
            "delta": self.delta,
        }


@dataclass
class SpendResult:
    upgrades: List[Upgrade]
    remaining: float
    reason: str = ""          # 花不掉时说明原因

    @property
    def spent(self) -> float:
        return round(sum(item.delta for item in self.upgrades), 2)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "upgrades": [item.as_dict() for item in self.upgrades],
            "spent": self.spent,
            "remaining": round(self.remaining, 2),
            "reason": self.reason,
        }


def _rows(
    find: FindAlternatives,
    product: Dict[str, Any],
    gear_name: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """先按子品类找同类款，再按**装备名**检索。

    用商品名检索是错的：搜"双人防水帐篷"只会命中它自己，永远找不到更贵的款。
    要搜"帐篷"这种品类词，才能看到同类的其他档位。
    """
    subcategory = str(product.get("subcategory") or "")
    rows: List[Dict[str, Any]] = []
    if subcategory:
        try:
            rows = list(find(subcategory=subcategory, limit=limit) or [])
        except Exception:
            rows = []
    for query in (gear_name, str(product.get("subcategory") or "")):
        if rows or not query:
            break
        try:
            rows = list(find(query=query, limit=limit) or [])
        except Exception:
            rows = []
    return [row for row in rows if isinstance(row, dict) and row.get("name")]


def _spec_ok(facts: TripFacts, item: GearItem, row: Dict[str, Any]) -> bool:
    """换上去的款必须仍然满足规格；规格未知直接否掉。"""
    for requirement in spec_requirements(facts, {item.name}):
        result = check_spec(requirement, as_specs(row.get("specs")))
        if result.status in (STATUS_INSUFFICIENT, STATUS_UNKNOWN):
            return False
    return True


def _deliverable(row: Dict[str, Any], days_left: Optional[int]) -> bool:
    if days_left is None:
        return True
    lead = row.get("lead_time_days")
    if lead is None:
        return True
    try:
        return int(lead) <= days_left
    except (TypeError, ValueError):
        return True


def upgrade_within_budget(
    facts: TripFacts,
    gear: List[GearItem],
    products: Dict[str, Dict[str, Any]],
    remaining: float,
    find_alternatives: Optional[FindAlternatives] = None,
    today: Optional[Any] = None,
) -> SpendResult:
    """把钱花掉：先给每件装备找出最贵且负担得起的升级，再按差价从大到小落地。

    先排序再花，而不是按装备顺序一路换——顺序依赖会让"先遇到的小件"吃掉额度，
    最后剩一堆钱花不出去，正好和"尽量用完"相反。

    ``products`` 原地更新：调用方拿到的就是升级后的选品，后续校验基于它。
    """
    upgrades: List[Upgrade] = []
    if not find_alternatives or remaining <= MIN_UPGRADE_DELTA:
        return SpendResult(upgrades, remaining, _why(remaining, find_alternatives))

    left_over = days_until(departure_date(facts), today)
    candidates: List[Tuple[float, GearItem, Dict[str, Any]]] = []

    for item in gear:
        if item.status != "need_buy":
            continue
        current = products.get(item.name)
        if not current:
            continue
        base_price = float(current.get("price") or 0) * item.qty
        best: Optional[Dict[str, Any]] = None
        best_delta = 0.0
        for row in _rows(find_alternatives, current, item.name, limit=20):
            if str(row.get("name")) == str(current.get("name")):
                continue
            if (row.get("stock") or 0) <= 0:
                continue
            delta = round(float(row.get("price") or 0) * item.qty - base_price, 2)
            if delta < MIN_UPGRADE_DELTA or delta > remaining:
                continue
            if not _spec_ok(facts, item, row):
                continue
            if not _deliverable(row, left_over):
                continue
            if delta > best_delta:
                best, best_delta = row, delta
        if best is not None:
            candidates.append((best_delta, item, best))

    # 差价大的先花：同样的额度里能塞进去的总额更高。
    for delta, item, best in sorted(candidates, key=lambda row: row[0], reverse=True):
        if delta > remaining:
            continue
        current = products[item.name]
        new_product = dict(best)
        new_product["match"] = {
            **dict(current.get("match") or {}),
            "upgraded_from": current.get("name"),
            "upgraded_from_price": current.get("price"),
        }
        products[item.name] = new_product
        upgrades.append(
            Upgrade(
                gear=item.name,
                from_name=str(current.get("name") or ""),
                from_price=float(current.get("price") or 0),
                to_name=str(best.get("name") or ""),
                to_price=float(best.get("price") or 0),
                qty=item.qty,
            )
        )
        remaining = round(remaining - delta, 2)

    return SpendResult(upgrades, remaining, _why(remaining, find_alternatives))


def _why(remaining: float, find: Optional[FindAlternatives]) -> str:
    """钱花不掉时给一句确切原因，而不是让用户猜。"""
    if not find:
        return "没有商品检索能力，无法升级"
    if remaining > MIN_UPGRADE_DELTA:
        return "库里没有更贵的可用款，没给你凑数"
    return ""
