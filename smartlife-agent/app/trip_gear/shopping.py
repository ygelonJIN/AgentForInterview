"""把装备缺口映射到现有商品库。

不重写检索：调用方注入搜索函数（默认 ProductRepository.search），
这里只负责把“睡袋、防潮垫”这类装备名换成能搜到的词，并按预算挑一个。
"""
from __future__ import annotations

import re
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
    # 模型补充的长尾装备名，收敛后映射到库里真实存在的品类
    "防水鞋或运动鞋": "登山鞋",
    "防水运动鞋": "登山鞋",
    "运动鞋": "登山鞋",
    "防水冲锋衣或雨衣": "冲锋衣",
    "冲锋衣": "冲锋衣",
    "雨衣": "雨伞",
    "折叠伞": "雨伞",
    "雨伞": "雨伞",
    "双肩小包": "背包",
    "背包防雨罩": "背包",
    "便携洗漱包": "洗漱用品",
    "洗漱包": "洗漱用品",
    "便携水杯": "保温杯",
    "水杯": "保温杯",
    "徒步鞋": "登山鞋",
    "抓绒衣": "羽绒",
    "薄羽绒外套": "羽绒",
    "抓绒衣或薄羽绒外套": "羽绒",
    "背包罩": "背包",
    "防水背包罩": "背包",
}

# 模型输出常见噪声：括号说明、"或"并列、顿号并列。检索前先剥掉。
_BRACKET_RE = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_SPLIT_RE = re.compile(r"[或/／、|]")


def normalize_gear_name(name: str) -> str:
    """把模型的自由装备名收敛成可检索的短名。

    ``便携洗漱包（分装洗护用品）`` → ``便携洗漱包``；
    ``防水冲锋衣或雨衣`` → ``防水冲锋衣``。
    展示仍用原名，这里只影响检索与品类判定。
    """
    text = _BRACKET_RE.sub("", str(name or ""))
    text = _SPLIT_RE.split(text)[0]
    return text.strip()


# 模型爱加的修饰前缀；剥掉后才能命中库里的真实品类。
_MODIFIER_PREFIXES = (
    "防滑", "防水", "防雨", "透气", "轻便", "轻量", "便携",
    "加厚", "专业", "速干", "薄款", "大容量", "折叠",
)


def strip_modifiers(name: str) -> str:
    """逐层剥掉修饰前缀：``防滑防水运动鞋`` → ``运动鞋``。"""
    text = normalize_gear_name(name)
    changed = True
    while changed:
        changed = False
        for prefix in _MODIFIER_PREFIXES:
            if text.startswith(prefix) and len(text) > len(prefix) + 1:
                text = text[len(prefix):]
                changed = True
    return text.strip()


def canonical_gear_name(name: str) -> str:
    """装备名的规范身份，用于判断"是不是同一件东西"。

    ``防水鞋`` 与 ``防滑防水运动鞋`` 都会收敛到 ``登山鞋``，
    从而在合并阶段识别为同一项，避免同一个商品被买两遍。
    """
    short = strip_modifiers(name)
    return ALIASES.get(short, short)


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
        """检索词顺序：原名 → 规范化短名 → 剥修饰短名 → 各自命中的别名。"""
        candidates: List[str] = []
        for raw in (
            item.name,
            normalize_gear_name(item.name),
            strip_modifiers(item.name),
        ):
            if raw and raw not in candidates:
                candidates.append(raw)
        for raw in list(candidates):
            alias = ALIASES.get(raw)
            if alias and alias not in candidates:
                candidates.append(alias)
        return candidates

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
