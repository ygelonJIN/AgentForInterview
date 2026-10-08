"""Cross-Encoder A/B 实验工具测试。"""

from app.evaluation.rerank_ab import ABResult, ABMetrics, assign_variant, choose_winner, run_paired_ab
from app.evaluation.rerank_eval import RankingCase


def _case(query):
    return RankingCase(
        query=query,
        documents=(
            {"id": "good", "content": query, "score": 0.1, "metadata": {}},
            {"id": "bad", "content": "无关内容", "score": 0.9, "metadata": {}},
        ),
        relevant_ids=("good",),
    )


def test_paired_ab_reports_delta_and_deterministic_assignment():
    cases = [_case("防水跑鞋"), _case("轻量登山鞋"), _case("保温杯"), _case("拍照手机")]

    def treatment(case):
        return list(case.documents)

    result = run_paired_ab(cases, treatment_ranker=treatment, k=2)

    assert result.control.cases == 4
    assert result.treatment.cases == 4
    assert result.delta["ndcg_at_k"] == 0.0
    assert set(result.control_case_ids) | set(result.treatment_case_ids) == {
        case.query for case in cases
    }
    assert assign_variant("same", salt="x") == assign_variant("same", salt="x")


def test_choose_winner_uses_primary_ndcg_delta():
    result = ABResult(
        k=5,
        control=ABMetrics(1, 1.0, 1.0, 0.5),
        treatment=ABMetrics(1, 1.0, 1.0, 0.7),
        delta={"recall_at_k": 0.0, "mrr_at_k": 0.0, "ndcg_at_k": 0.2},
        control_case_ids=(),
        treatment_case_ids=(),
    )
    assert choose_winner(result, min_ndcg_delta=0.1) == "treatment"
