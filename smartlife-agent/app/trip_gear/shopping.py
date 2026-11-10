"""把装备需求映射到现有商品库。

**这里不再维护"装备名 -> 商品名"的别名表。** 只要开始维护，商品一多就维护不动：
库里有没有货，由检索本身回答。检索不到的需求直接不进清单（"有什么推荐什么"），
而不是先列一堆买不到的东西再回头核对。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.trip_gear.models import GearItem
from app.trip_gear.select import pick as pick_scored
from app.trip_gear.spec_schema import subcategory_for

# 模型输出常见噪声：括号说明、"或"并列、顿号并列。检索前先剥掉。
_BRACKET_RE = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_SPLIT_RE = re.compile(r"[或/／、|]")

# 模型爱加的修饰前缀；剥掉后才能命中库里的真实品类。
_MODIFIER_PREFIXES = (
    "防滑", "防水", "防雨", "透气", "轻便", "轻量", "便携",
    "加厚", "专业", "速干", "薄款", "大容量", "折叠",
)


def normalize_gear_name(name: str) -> str:
    """把模型的自由装备名收敛成可检索的短名。

    ``便携洗漱包（分装洗护用品）`` → ``便携洗漱包``；
    ``防水冲锋衣或雨衣`` → ``防水冲锋衣``。
    """
    text = _BRACKET_RE.sub("", str(name or ""))
    text = _SPLIT_RE.split(text)[0]
    return text.strip()


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
    """装备名的规范身份，用于判断"是不是同一件东西"（只做词形归一，不查别名表）。"""
    return strip_modifiers(name)


class GearShopper:
    def __init__(
        self,
        search: Optional[Callable[..., List[dict]]] = None,
        preferred_brands: Optional[List[str]] = None,
        facts: Any = None,
        review_lookup: Optional[Callable[[str], Dict[str, Any]]] = None,
    ):
        self.search = search
        self.preferred_brands = list(preferred_brands or [])
        # 给了 facts 就走打分选品（参数+口碑+预算），否则退回按价格挑。
        self.facts = facts
        # 只在打分平局时被调用：用真实评价裁决，不参与打分。
        self.review_lookup = review_lookup

    # ---------------------------------------------------------------- 对外

    def fill(
        self,
        gear: List[GearItem],
        products: Dict[str, Dict[str, Any]],
        remaining_budget: Optional[float] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """给每一项挑一个真实商品。

        - 挑到 → 留在清单里，带上商品
        - 挑不到（库里没有/无货）→ **从清单里移除**，不留空行、不报数
        - 挑得到但买不起 → 留在清单里并注明"预算不足，未选款"
        """
        if not self.search:
            return products

        kept: List[GearItem] = []
        for item in gear:
            if item.status != "need_buy" or item.name in products:
                kept.append(item)
                continue
            product, reason = self._select(item, remaining_budget)
            if product:
                products[item.name] = product
                if remaining_budget is not None:
                    remaining_budget -= float(product.get("price") or 0) * item.qty
                kept.append(item)
                continue
            if reason == "budget":
                item.reason = (item.reason + "；预算不足，未选款").strip("；")
                kept.append(item)
            # reason == "no_match"：库里没有，直接不进清单
        gear[:] = kept
        return products

    def candidates_for(self, item: GearItem) -> List[Dict[str, Any]]:
        """公开候选集（模型挑款复用同一批候选，避免两套检索）。"""
        return self._candidates(item)

    def _queries(self, item: GearItem) -> List[str]:
        """检索词顺序：原名 → 规范化短名 → 剥修饰短名。"""
        candidates: List[str] = []
        for raw in (
            item.name,
            normalize_gear_name(item.name),
            strip_modifiers(item.name),
        ):
            if raw and raw not in candidates:
                candidates.append(raw)
        return candidates

    # ---------------------------------------------------------------- 选品

    def _candidates(self, item: GearItem) -> List[Dict[str, Any]]:
        """把库里能搜到的候选一次性捞出来；同一商品只留一行。"""
        rows: List[Dict[str, Any]] = []
        seen = set()
        for query in self._queries(item):
            try:
                found = list(self.search(query=query, limit=30) or [])
            except Exception:
                found = []
            for row in found:
                if not isinstance(row, dict) or not row.get("name"):
                    continue
                if (row.get("stock") or 0) <= 0:
                    continue
                identity = row.get("id") or (
                    row.get("name"), row.get("brand"), row.get("price")
                )
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(row)
            if len(rows) >= 40:
                break

        subcategory = subcategory_for(item.name)
        if subcategory:
            narrowed = [row for row in rows if row.get("subcategory") == subcategory]
            rows = narrowed or rows
        return rows

    def _select(
        self,
        item: GearItem,
        remaining_budget: Optional[float],
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """返回 (商品, 原因)。原因只用于区分"库里没有"和"买不起"。"""
        candidates = self._candidates(item)
        if not candidates:
            return None, "no_match"

        if self.facts is not None:
            best = pick_scored(
                candidates,
                category=item.name,
                gear_name=item.name,
                facts=self.facts,
                remaining=remaining_budget,
                qty=item.qty,
                preferred_brands=self.preferred_brands,
                review_lookup=self.review_lookup,
            )
            if best is not None:
                product = dict(best.product)
                product["match"] = self._match_meta(item, product)
                product["match"]["score"] = best.score
                return product, "ok"
        else:
            rows = sorted(candidates, key=self._sort_key)
            chosen = rows[0]
            if remaining_budget is not None:
                affordable = [
                    row for row in rows
                    if float(row.get("price") or 0) * item.qty <= remaining_budget
                ]
                chosen = affordable[0] if affordable else None
            if chosen is not None:
                product = dict(chosen)
                product["match"] = self._match_meta(item, product)
                return product, "ok"

        # 有候选但一个都买不起 → 预算问题；否则是规格不达标，按库里没有处理
        affordable_any = remaining_budget is None or any(
            float(row.get("price") or 0) * item.qty <= remaining_budget
            for row in candidates
        )
        return None, ("no_match" if affordable_any else "budget")

    def _pick(
        self,
        item: GearItem,
        remaining_budget: Optional[float],
    ) -> Optional[Dict[str, Any]]:
        """兼容旧调用点：只要商品。"""
        return self._select(item, remaining_budget)[0]

    def _sort_key(self, row: Dict[str, Any]) -> tuple:
        price = float(row.get("price") or 0)
        brand = str(row.get("brand") or "")
        preferred = bool(self.preferred_brands) and brand in self.preferred_brands
        # 偏好品牌相当于把价格放宽 10% 参与同档比较，不至于为了品牌多花一倍钱。
        return (round(price * 0.9, 2) if preferred else price, -float(row.get("rating") or 0))

    def _match_meta(self, item: GearItem, product: Dict[str, Any]) -> Dict[str, Any]:
        """记录这次命中是"同名"还是"同类替代"；替代时写清按什么词搜到的。"""
        query = normalize_gear_name(item.name)
        product_name = str(product.get("name") or "")
        exact = bool(query) and query in product_name
        brand = str(product.get("brand") or "")
        return {
            "gear": item.name,
            "query": query,
            "kind": "exact" if exact else "substitute",
            "by_preference": bool(self.preferred_brands) and brand in self.preferred_brands,
        }
