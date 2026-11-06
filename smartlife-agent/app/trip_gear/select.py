"""打分选品：从"最便宜"改成"这个预算里最合适的"。

旧逻辑按价格升序取第一个——同价位里参数更好、口碑更好的款会被跳过，
10℃ 露营会选到刚卡线的 5℃ 棉睡袋，而不是明显更合适的款。

打分只做三件事，权重固定、可解释：

    score = 0.5 × 参数匹配 + 0.3 × 口碑 − 0.2 × 预算占用

- **参数匹配**：先过硬门槛（温标/容量/防水不达标直接出局），再按品类参数表
  归一化打分（羽绒服看蓬松度与充绒量，手机看 CPU 与内存）；
- **口碑**：评分在同一批候选里归一化；
- **预算占用**：同样的钱能买到更好时不浪费。

规格未知不直接出局，但参数匹配只给一半分，并且最终进入 `verify` 时会被要求确认——
系统不替用户假设"应该够用"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.trip_gear.spec_match import (
    STATUS_INSUFFICIENT,
    STATUS_UNKNOWN,
    as_specs,
    check as check_spec,
    spec_requirements,
)
from app.trip_gear.spec_schema import quality_score, ranges_from

WEIGHT_FIT = 0.45
WEIGHT_QUALITY = 0.25
WEIGHT_RATING = 0.20
WEIGHT_PRICE = 0.15
BRAND_BONUS = 0.06

# 评分按固定刻度归一化，不用候选内 min-max：否则在一堆 4.8 里，4.8 会被算成 0 分。
RATING_FLOOR = 3.5
RATING_CEIL = 5.0

# 这两类参数"越大越好"是错的：两人出行不需要四人帐篷，两天的行程也不需要 65L 包。
# 对它们按"接近需求"打分，超出需求反而扣分。
CLOSENESS_KEYS = {"capacity_persons", "volume_l"}


@dataclass
class Scored:
    product: Dict[str, Any]
    score: float
    fit: float
    quality: float
    rating_norm: float
    price_ratio: float
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.product.get("name"),
            "price": self.product.get("price"),
            "score": round(self.score, 4),
            "fit": round(self.fit, 3),
            "quality": round(self.quality, 3),
            "reason": self.reason,
        }


def _fit(product: Dict[str, Any], requirements: Sequence) -> tuple:
    """参数匹配度：硬伤直接出局，达标看余量，未知给一半分。

    只看"达标/不达标"会让刚好卡线的款和余量充足的款同分（10℃ 的夜里，
    5℃ 和 -15℃ 睡袋一样得分），于是价格成了唯一区别——这又退回"只挑便宜"。
    所以达标的还要看余量：温标低得越多、容量富余越大，分越高。
    """
    if not requirements:
        return 1.0, ""
    specs = as_specs(product.get("specs"))
    parts: List[float] = []
    for requirement in requirements:
        result = check_spec(requirement, specs)
        if result.status == STATUS_INSUFFICIENT:
            return 0.0, result.hint or f"不满足 {requirement.describe()}"
        if result.status == STATUS_UNKNOWN:
            parts.append(0.5)
            continue
        actual = float(result.actual or 0)
        span = max(1.0, abs(requirement.value) + 5.0)
        slack = (
            (requirement.value - actual) / span
            if requirement.op == "<="
            else (actual - requirement.value) / span
        )
        part = 0.7 + 0.3 * min(1.0, max(0.0, slack))
        if requirement.key in CLOSENESS_KEYS:
            overshoot = (actual - requirement.value) / max(1.0, abs(requirement.value))
            part = max(0.3, part - 0.35 * max(0.0, overshoot))
        parts.append(part)
    return round(sum(parts) / len(parts), 4), ""


def _rating_norm(rating: float) -> float:
    span = RATING_CEIL - RATING_FLOOR
    return round(min(1.0, max(0.0, (float(rating or 0) - RATING_FLOOR) / span)), 4)


def rank(
    candidates: Iterable[Dict[str, Any]],
    category: str,
    gear_name: str,
    facts: Any = None,
    remaining: Optional[float] = None,
    qty: int = 1,
    preferred_brands: Optional[Sequence[str]] = None,
) -> List[Scored]:
    """给候选打分并降序排列；不可行的直接剔除。"""
    pool = [
        product for product in candidates
        if product.get("name") and (product.get("stock") or 0) > 0
    ]
    if not pool:
        return []

    requirements = spec_requirements(facts, {gear_name}) if facts is not None else []
    ranges = ranges_from(pool, category)

    scored: List[Scored] = []
    for product in pool:
        price = float(product.get("price") or 0) * max(1, qty)
        if remaining is not None and price > remaining:
            continue
        fit, why = _fit(product, requirements)
        if fit <= 0:
            continue
        quality = quality_score(category, as_specs(product.get("specs")), ranges)
        quality = 0.5 if quality is None else quality
        rating_norm = _rating_norm(product.get("rating") or 0)
        price_ratio = 0.0 if not remaining else min(1.0, price / remaining)

        score = (
            WEIGHT_FIT * fit
            + WEIGHT_QUALITY * quality
            + WEIGHT_RATING * rating_norm
            - WEIGHT_PRICE * price_ratio
        )
        brand = str(product.get("brand") or "")
        if preferred_brands and brand in preferred_brands:
            score += BRAND_BONUS

        scored.append(
            Scored(
                product=product,
                score=round(score, 4),
                fit=fit,
                quality=quality,
                rating_norm=rating_norm,
                price_ratio=price_ratio,
                reason=why,
            )
        )

    scored.sort(key=lambda item: item.score, reverse=True)
    return scored


def pick(
    candidates: Iterable[Dict[str, Any]],
    category: str,
    gear_name: str,
    facts: Any = None,
    remaining: Optional[float] = None,
    qty: int = 1,
    preferred_brands: Optional[Sequence[str]] = None,
) -> Optional[Scored]:
    ranked = rank(candidates, category, gear_name, facts, remaining, qty, preferred_brands)
    return ranked[0] if ranked else None
