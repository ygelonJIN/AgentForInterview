"""把装备缺口映射到现有商品库。

不重写检索：调用方注入搜索函数（默认 ProductRepository.search），
这里只负责把“睡袋、防潮垫”这类装备名换成能搜到的词，并按预算挑一个。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from app.trip_gear.models import GearItem

# 装备名 -> 商品库里的检索词。只做语义安全的映射，搜不到就如实留空。
ALIASES = {
    "雨具": "雨伞",
    "防水鞋": "登山鞋",
    "双肩包": "背包",
    "保暖层": "羽绒",
    "换洗衣物": "T恤",
    "儿童衣物": "T恤",
    "裙子": "连衣裙",
    "裙装": "连衣裙",
}


class GearShopper:
    def __init__(self, search: Optional[Callable[..., List[dict]]] = None):
        self.search = search

    def fill(
        self,
        gear: List[GearItem],
        products: Dict[str, Dict[str, Any]],
        remaining_budget: Optional[float] = None,
    ) -> Dict[str, Dict[str, Any]]:
        if not self.search:
            return products
        for item in gear:
            if item.status != "need_buy" or item.name in products:
                continue
            product = self._pick(item, remaining_budget)
            if not product:
                continue
            products[item.name] = product
            if remaining_budget is not None:
                remaining_budget -= float(product.get("price") or 0) * item.qty
        return products

    def _queries(self, item: GearItem) -> List[str]:
        alias = ALIASES.get(item.name)
        queries = [alias, item.name] if alias and alias != item.name else [item.name]
        return [query for query in queries if query]

    def _pick(
        self,
        item: GearItem,
        remaining_budget: Optional[float],
    ) -> Optional[Dict[str, Any]]:
        for query in self._queries(item):
            try:
                rows = list(self.search(query=query, limit=5) or [])
            except Exception:
                rows = []
            rows = [row for row in rows if isinstance(row, dict) and row.get("name")]
            if not rows:
                continue
            rows = [row for row in rows if (row.get("stock") or 0) > 0] or rows
            rows.sort(key=lambda row: float(row.get("price") or 0))
            if remaining_budget is not None:
                affordable = [
                    row for row in rows
                    if float(row.get("price") or 0) * item.qty <= remaining_budget
                ]
                if affordable:
                    return affordable[0]
            return rows[0]
        return None
