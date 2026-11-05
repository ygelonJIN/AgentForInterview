"""统一代价：把"改什么"和"问什么"放到同一把尺子上。

三条设计约束，改动前先读：

1. **纯函数**。不读模型、不读库、不碰网络，所有输入都是已解析的对象；
   这样代价模型可以单测、可以在评测里复算，也不受模型波动影响。
2. **两种幅度分开**。判定用归一化幅度 ``capped ∈ [0, 1]``（"超没超、
   超得算不算严重"）；排序用未归一化幅度 ``raw``（"哪个更疼"）。
   只用 capped 会让超支 2 倍和 3 倍代价相同，修复会提前停住。
3. **每一项都带来源**。代价不是黑箱数字，而是 ``CostTerm`` 列表，
   可以逐项展示"这条约束贡献了多少"，答辩和界面都用它。

    cost(λ) = Σ w_i · δ_i
    w_i = 硬度权重（hard 1.0 / soft 0.6）× 来源置信度
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Dict, Iterable, List, NamedTuple, Optional, Tuple

# 来源置信度：先说出来的比推出来的可信，模型推断最弱。
# 用前缀匹配，因为约束来源里常带说明，如"规则推导（夜间温度留 5℃ 余量）"。
SOURCE_CONFIDENCE_PREFIX: Tuple[Tuple[str, float], ...] = (
    ("用户", 1.0),
    ("天气", 0.9),
    ("工具", 0.9),
    ("外部", 0.9),
    ("规则", 0.8),
    ("模型", 0.5),
)
DEFAULT_CONFIDENCE = 0.7

# 硬度权重：硬约束不可松弛，权重只是它"被违反时有多严重"的刻度。
HARD_WEIGHT = 1.0
SOFT_WEIGHT = 0.6

# 缺失类需求的幅度：按"缺了会怎样"分三档，复用既有的可选/保护名单，
# 不另建一套分类，避免两处口径打架。
DELTA_PROTECTED = 1.0
DELTA_NORMAL = 0.5
DELTA_OPTIONAL = 0.2

# 不确定项的幅度：说不清就少算一点，别让"未标注温标"顶掉真正的缺失。
DELTA_UNVERIFIED = 0.3

# 自动松弛的容忍上限。只在"自动改还是问用户"这一层把关，
# 不参与可行性判定——可行与否只看硬约束满不满足。
DEFAULT_TAU = 0.15


def source_confidence(source: str) -> float:
    """按来源前缀给出置信度；认不出来的按默认值处理。"""
    text = str(source or "").strip()
    for prefix, value in SOURCE_CONFIDENCE_PREFIX:
        if text.startswith(prefix):
            return value
    return DEFAULT_CONFIDENCE


def constraint_weight(hard: bool, source: str) -> float:
    """约束的权重 = 硬度权重 × 来源置信度。"""
    return (HARD_WEIGHT if hard else SOFT_WEIGHT) * source_confidence(source)


class Delta(NamedTuple):
    """一次违反的幅度：capped 用于判定，raw 用于排序。"""

    capped: float
    raw: float


def budget_delta(actual: float, limit: float) -> Delta:
    """预算类违反幅度。

    预算为 0 或负数时无法计算比例，直接按"超了就是满幅"处理，
    避免除零，也避免把非法输入算成 0 代价。
    """
    actual = float(actual or 0.0)
    limit = float(limit or 0.0)
    if limit <= 0:
        if actual <= 0:
            return Delta(0.0, 0.0)
        return Delta(1.0, actual)
    if actual <= limit:
        return Delta(0.0, 0.0)
    raw = (actual - limit) / limit
    return Delta(min(1.0, raw), raw)


def missing_delta(
    name: str,
    optional: Collection[str],
    protected: Collection[str],
) -> float:
    """缺失一项装备/能力的幅度。

    保护名单里的东西缺了等于方案不成立，满分；既有的可选清单里缺了
    只是少点舒服，压低；其余算功能必需。
    """
    if name in protected:
        return DELTA_PROTECTED
    if name in optional:
        return DELTA_OPTIONAL
    return DELTA_NORMAL


def gear_missing_delta(name: str) -> float:
    """``missing_delta`` 的便捷版，名单从既有模块取。

    延迟导入：``gear`` 与 ``repair`` 都依赖 ``models``，这里若在模块级
    导入会绕成环，也把代价模块绑死在两个业务模块的初始化顺序上。
    """
    from app.trip_gear.gear import OPTIONAL
    from app.trip_gear.repair import PROTECTED_SKIP

    return missing_delta(name, OPTIONAL, PROTECTED_SKIP)


@dataclass(frozen=True)
class CostTerm:
    """代价表里的一项：谁、多重要、违反多少、折算后多少。"""

    label: str
    weight: float
    delta: float
    capped: bool = True
    detail: str = ""

    @property
    def cost(self) -> float:
        return round(self.weight * self.delta, 6)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "weight": round(self.weight, 4),
            "delta": round(self.delta, 4),
            "cost": round(self.cost, 4),
            "capped": self.capped,
            "detail": self.detail,
        }


def total(terms: Iterable[CostTerm]) -> float:
    """总代价。"""
    return round(sum(term.cost for term in terms), 6)


def explain(terms: Iterable[CostTerm], limit: int = 8) -> List[Dict[str, Any]]:
    """按贡献降序给出可展示的代价明细。"""
    ordered = sorted(terms, key=lambda term: term.cost, reverse=True)
    return [term.as_dict() for term in ordered[:limit]]


def capped_total(terms: Iterable[CostTerm]) -> float:
    """只统计归一化项的总代价，供"是否可接受"的判断使用。"""
    return total(term for term in terms if term.capped)


def make_term(
    label: str,
    *,
    hard: bool,
    source: str,
    delta: Delta,
    detail: str = "",
) -> CostTerm:
    """把一条约束的违反折算成代价项；判定用 capped，排序取 raw。"""
    weight = constraint_weight(hard, source)
    return CostTerm(
        label=label,
        weight=weight,
        delta=delta.capped,
        capped=True,
        detail=detail,
    )


def make_sort_term(
    label: str,
    *,
    hard: bool,
    source: str,
    delta: Delta,
    detail: str = "",
) -> CostTerm:
    """排序用的代价项：保留未归一化幅度，避免高额超支被抹平。"""
    weight = constraint_weight(hard, source)
    return CostTerm(
        label=label,
        weight=weight,
        delta=delta.raw,
        capped=False,
        detail=detail,
    )


def acceptable(terms: Iterable[CostTerm], tau: float = DEFAULT_TAU) -> bool:
    """总代价是否落在可自动松弛的范围内。"""
    return capped_total(terms) <= float(tau) + 1e-9


def parse_tau(raw: Optional[Any]) -> float:
    """从状态里读 τ，非法值回落到默认，不让配置错误改变判定结果。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TAU
    if value < 0:
        return DEFAULT_TAU
    return value
