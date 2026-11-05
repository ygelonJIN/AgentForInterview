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
    "儿童衣物": "儿童",
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
    def __init__(
        self,
        search: Optional[Callable[..., List[dict]]] = None,
        preferred_brands: Optional[List[str]] = None,
    ):
        self.search = search
        self.preferred_brands = list(preferred_brands or [])

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
        """挑一个商品；挑不到就返回 None，绝不拿库里没有的东西顶上。

        两个硬规则：
        1. **无货不当候选**。旧实现写的是 ``... if stock > 0] or rows``，
           全都没货时又把整表退回来，等于把无货商品当成了推荐。
        2. **近似替代要标出来**。库里没有"保暖层"，命中"轻薄羽绒外套"
           是合理的，但必须注明这是替代，否则用户会以为库里真有这件东西。
        """
        for query in self._queries(item):
            try:
                rows = list(self.search(query=query, limit=5) or [])
            except Exception:
                rows = []
            rows = [
                row for row in rows
                if isinstance(row, dict) and row.get("name") and (row.get("stock") or 0) > 0
            ]
            if not rows:
                continue
            # 品牌偏好只做同档优先：先按价格分档（10% 内视为同档），再让偏好品牌胜出。
            rows.sort(key=lambda row: self._sort_key(row))
            chosen = None
            if remaining_budget is not None:
                affordable = [
                    row for row in rows
                    if float(row.get("price") or 0) * item.qty <= remaining_budget
                ]
                chosen = affordable[0] if affordable else None
            if chosen is None:
                chosen = rows[0]
            product = dict(chosen)
            product["match"] = self._match_meta(item, product, query)
            return product
        return None

    def _sort_key(self, row: Dict[str, Any]) -> tuple:
        price = float(row.get("price") or 0)
        brand = str(row.get("brand") or "")
        preferred = bool(self.preferred_brands) and brand in self.preferred_brands
        # 偏好品牌相当于把价格放宽 10% 参与同档比较，不至于为了品牌多花一倍钱。
        return (round(price * 0.9, 2) if preferred else price, -float(row.get("rating") or 0))

    def _match_meta(self, item: GearItem, product: Dict[str, Any], query: str) -> Dict[str, Any]:
        """记录这次命中是"同名"还是"替代"，以及替代用的是什么词。"""
        name = normalize_gear_name(item.name)
        product_name = str(product.get("name") or "")
        exact = bool(name) and name in product_name
        brand = str(product.get("brand") or "")
        meta = {
            "gear": item.name,
            "query": query,
            "kind": "exact" if exact else "substitute",
            "alias": "",
            "by_preference": bool(self.preferred_brands) and brand in self.preferred_brands,
        }
        if exact:
            return meta
        # 替代时报告“换成了库里哪个品类”，而不是把用户的词重复一遍。
        alias = (
            ALIASES.get(strip_modifiers(item.name))
            or ALIASES.get(normalize_gear_name(item.name))
            or query
        )
        meta["alias"] = alias
        return meta
