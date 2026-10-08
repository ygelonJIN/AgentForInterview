"""统一 RetrievalService 契约测试。"""

import threading

import pytest
import time

from app.observability import get_trace_recorder
from app.retrieval.rag import RetrievalResult
from app.retrieval.service import RetrievalService


class _SqlSpy:
    def __init__(self):
        self.calls = []

    def query(self, query):
        self.calls.append(query)
        return {
            "sql": "SELECT * FROM products",
            "explanation": "查找商品",
            "needs_rag": ["评价"],
            "results": [{
                "id": 7,
                "name": "跑鞋",
                "description": "缓震",
                "price": 399,
                "brand": "Brand",
            }],
            "count": 1,
        }


class _RagSpy:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def retrieve(self, query, k, owner=None):
        self.calls.append((query, k))
        return RetrievalResult(
            documents=[{
                "content": "评价：缓震很好",
                "metadata": {"product_id": 7},
                "score": 0.1,
            }],
            scores=[0.1],
            diagnostics=[] if self.ok else ["RAG 检索失败"],
            ok=self.ok,
            error=None if self.ok else "vector store missing",
        )


class _RerankerSpy:
    def __init__(self):
        self.calls = []

    def rerank(self, query, documents, top_n):
        self.calls.append((query, len(documents), top_n))
        return list(reversed(documents))[:top_n]


def test_mixed_retrieval_combines_sql_rag_reviews_and_diagnostics():
    sql = _SqlSpy()
    rag = _RagSpy()
    reranker = _RerankerSpy()
    service = RetrievalService(sql, rag, reranker)

    result = service.retrieve("推荐跑鞋和评价", strategy="mixed", top_k=5)

    assert result["strategy"] == "mixed"
    assert result["sql"].startswith("SELECT")
    assert result["products"][0]["name"] == "跑鞋"
    assert result["products"][0]["related_reviews"] == ["评价：缓震很好"]
    assert result["documents"][0]["content"] == "评价：缓震很好"
    assert result["source"] == {"sql": "sqlite", "rag": "chroma"}
    assert sql.calls == ["推荐跑鞋和评价"]
    assert rag.calls == [("推荐跑鞋和评价 评价", 20)]
    assert len(reranker.calls) == 2


def test_strategy_controls_sql_and_rag_calls():
    sql = _SqlSpy()
    rag = _RagSpy()
    service = RetrievalService(sql, rag, reranker=_RerankerSpy())

    service.retrieve("推荐跑鞋", strategy="sql_only")
    assert len(sql.calls) == 1
    assert rag.calls == []

    service.retrieve("评价怎么样", strategy="rag_only")
    assert len(sql.calls) == 1
    assert len(rag.calls) == 1


def test_rag_failure_is_explicit_and_no_review_context_is_fabricated():
    service = RetrievalService(_SqlSpy(), _RagSpy(ok=False), reranker=_RerankerSpy())

    result = service.retrieve("跑鞋评价", strategy="rag_only")

    assert result["rag_ok"] is False
    assert result["rag_error"] == "vector store missing"
    assert result["documents"] == []
    assert "RAG 检索失败" in result["diagnostics"]


def test_keyword_reranker_is_deterministic_fallback():
    class NoRag:
        def retrieve(self, query, k):
            return RetrievalResult(documents=[
                {"content": "手机", "metadata": {}, "score": 0.0},
                {"content": "跑步鞋 缓震", "metadata": {}, "score": 0.0},
            ])

    service = RetrievalService(_SqlSpy(), NoRag(), reranker=None)
    result = service.retrieve("跑步鞋 缓震", strategy="rag_only", top_k=2)

    assert result["documents"][0]["content"] == "跑步鞋 缓震"
    assert result["documents"][0]["rerank_score"] == 1.0


def test_vector_score_is_preserved_before_keyword_tiebreak():
    class ScoredRag:
        def retrieve(self, query, k):
            return RetrievalResult(documents=[
                {"content": "query exact", "metadata": {}, "score": 0.3},
                {"content": "other", "metadata": {}, "score": 0.1},
            ])

    service = RetrievalService(_SqlSpy(), ScoredRag(), reranker=None)
    result = service.retrieve("query", strategy="rag_only", top_k=2)

    assert [item["content"] for item in result["documents"]] == ["other", "query exact"]
    assert result["documents"][0]["rerank_source"] == "vector_score_keyword_tiebreak"
    assert result["rerank"]["documents"] == "vector_score_keyword_tiebreak"


def test_rag_evaluator_requires_real_judge_for_production_metrics():
    from app.evaluation.rag_evaluator import EvaluationConfigurationError, RAGEvaluator

    evaluator = RAGEvaluator()
    evaluator.add_test_case("问题", "答案事实", "上下文", "回答")

    with pytest.raises(EvaluationConfigurationError):
        evaluator.evaluate_all()


def test_rerank_ab_cache_is_separated_by_variant():
    class CountingReranker:
        def __init__(self):
            self.calls = 0

        def rerank(self, query, documents, top_n):
            self.calls += 1
            return list(documents)[:top_n]

    rag = _RagSpy()
    reranker = CountingReranker()
    service = RetrievalService(_SqlSpy(), rag, reranker)
    service.retrieve("跑鞋评价", strategy="rag_only", rerank_variant="control")
    service.retrieve("跑鞋评价", strategy="rag_only", rerank_variant="treatment")

    assert reranker.calls == 1
    assert service.cache_stats()["misses"] == 2
