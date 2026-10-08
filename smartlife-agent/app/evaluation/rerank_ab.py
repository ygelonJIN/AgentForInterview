"""Cross-Encoder 与基线排序的可复现实验和 A/B 分流。"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from app.evaluation.rerank_eval import (
    RankingCase,
    calculate_metrics,
    evaluate_strategy,
    rank_vector,
)


Ranker = Callable[[RankingCase], Sequence[Mapping[str, Any]]]


@dataclass(frozen=True)
class ABMetrics:
    cases: int
    recall_at_k: float
    mrr_at_k: float
    ndcg_at_k: float


@dataclass(frozen=True)
class ABResult:
    k: int
    control: ABMetrics
    treatment: ABMetrics
    delta: Dict[str, float]
    control_case_ids: tuple[str, ...]
    treatment_case_ids: tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "k": self.k,
            "control": asdict(self.control),
            "treatment": asdict(self.treatment),
            "delta": dict(self.delta),
            "control_case_ids": list(self.control_case_ids),
            "treatment_case_ids": list(self.treatment_case_ids),
        }


def assign_variant(case_id: str, *, salt: str = "rerank-v1") -> str:
    digest = hashlib.sha256(f"{salt}:{case_id}".encode("utf-8")).hexdigest()
    return "control" if int(digest[:8], 16) % 2 == 0 else "treatment"


def _metrics_for(cases: Sequence[RankingCase], ranker: Ranker, k: int) -> ABMetrics:
    metrics = [calculate_metrics(ranker(case), case.relevant_ids, k) for case in cases]
    if not metrics:
        return ABMetrics(0, 0.0, 0.0, 0.0)
    return ABMetrics(
        cases=len(metrics),
        recall_at_k=sum(item.recall_at_k for item in metrics) / len(metrics),
        mrr_at_k=sum(item.mrr_at_k for item in metrics) / len(metrics),
        ndcg_at_k=sum(item.ndcg_at_k for item in metrics) / len(metrics),
    )


def run_paired_ab(
    cases: Sequence[RankingCase],
    *,
    control_ranker: Optional[Ranker] = None,
    treatment_ranker: Ranker,
    k: int = 5,
    salt: str = "rerank-v1",
) -> ABResult:
    if not cases:
        raise ValueError("cases 不能为空")
    if k <= 0:
        raise ValueError("k 必须大于 0")
    control_ranker = control_ranker or rank_vector
    control_cases = []
    treatment_cases = []
    for index, case in enumerate(cases):
        case_id = case.query or f"case-{index}"
        if assign_variant(case_id, salt=salt) == "control":
            control_cases.append(case)
        else:
            treatment_cases.append(case)

    # 两侧都保留完整样本，分流字段用于真实在线流量分析；
    # 离线 paired 指标仍对全部样本比较，避免小样本分流偏差。
    control_metrics = _metrics_for(list(cases), control_ranker, k)
    treatment_metrics = _metrics_for(list(cases), treatment_ranker, k)
    delta = {
        "recall_at_k": treatment_metrics.recall_at_k - control_metrics.recall_at_k,
        "mrr_at_k": treatment_metrics.mrr_at_k - control_metrics.mrr_at_k,
        "ndcg_at_k": treatment_metrics.ndcg_at_k - control_metrics.ndcg_at_k,
    }
    return ABResult(
        k=k,
        control=control_metrics,
        treatment=treatment_metrics,
        delta=delta,
        control_case_ids=tuple(case.query for case in control_cases),
        treatment_case_ids=tuple(case.query for case in treatment_cases),
    )


def choose_winner(
    result: ABResult,
    *,
    min_ndcg_delta: float = 0.0,
) -> str:
    if result.delta["ndcg_at_k"] > min_ndcg_delta:
        return "treatment"
    if result.delta["ndcg_at_k"] < -min_ndcg_delta:
        return "control"
    return "tie"
