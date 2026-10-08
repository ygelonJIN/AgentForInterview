"""Rerank 离线指标和策略对比测试。"""

import json

import pytest

from app.evaluation.rerank_eval import (
    RerankEvalError,
    calculate_metrics,
    evaluate_rerank_cases,
    load_cases,
    parse_case,
    rank_cross_encoder,
    rank_keyword,
    rank_vector,
)


def _case():
    return parse_case({
        "query": "防水 跑步鞋",
        "documents": [
            {"id": "relevant", "content": "防水跑步鞋", "score": 0.3},
            {"id": "irrelevant", "content": "拍照手机", "score": 0.1},
        ],
        "relevant_ids": ["relevant"],
    })


def test_vector_and_keyword_ranking_expose_quality_difference():
    case = _case()

    vector_ranking = rank_vector(case)
    keyword_ranking = rank_keyword(case)

    assert [item["id"] for item in vector_ranking] == ["irrelevant", "relevant"]
    assert [item["id"] for item in keyword_ranking] == ["relevant", "irrelevant"]
    assert calculate_metrics(vector_ranking, case.relevant_ids, 2).mrr_at_k == 0.5
    assert calculate_metrics(keyword_ranking, case.relevant_ids, 2).mrr_at_k == 1.0


def test_evaluate_cases_compares_all_available_strategies():
    class _Reranker:
        def rerank(self, query, documents, top_n):
            return sorted(
                documents,
                key=lambda item: item["id"] != "relevant",
            )[:top_n]

    result = evaluate_rerank_cases(
        [_case(), _case()],
        k=2,
        cross_encoder_reranker=_Reranker(),
    )

    assert result["case_count"] == 2
    assert set(result["strategies"]) == {"vector", "keyword", "cross_encoder"}
    assert result["strategies"]["keyword"]["mrr@2"] == 1.0
    assert result["strategies"]["cross_encoder"]["mrr@2"] == 1.0


def test_cross_encoder_ranking_preserves_document_identity():
    class _Reranker:
        def rerank(self, query, documents, top_n):
            return list(reversed(documents))[:top_n]

    ranked = rank_cross_encoder(_case(), _Reranker())
    assert [item["id"] for item in ranked] == ["irrelevant", "relevant"]


def test_dataset_validation_rejects_unknown_relevant_ids(tmp_path):
    with pytest.raises(RerankEvalError, match="不存在的文档"):
        parse_case({
            "query": "查询",
            "documents": [{"id": "a", "content": "A", "score": 0.1}],
            "relevant_ids": ["missing"],
        })


def test_load_cases_reports_jsonl_line_numbers(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        '{"query":"ok","documents":[{"id":"a","content":"A"}],"relevant_ids":["a"]}\n'
        '{"query":""}\n',
        encoding="utf-8",
    )

    with pytest.raises(RerankEvalError, match=r":2:"):
        load_cases(path)


def test_keyword_ranking_handles_chinese_queries_without_spaces():
    case = parse_case({
        "query": "防水透气跑步鞋",
        "documents": [
            {"id": "irrelevant", "content": "拍照手机夜景清晰"},
            {"id": "relevant", "content": "防水跑步鞋，适合雨天跑步"},
        ],
        "relevant_ids": ["relevant"],
    })

    ranked = rank_keyword(case)

    assert ranked[0]["id"] == "relevant"
