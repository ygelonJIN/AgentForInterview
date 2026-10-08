"""统一 RetrievalService 契约测试。"""

import threading
import time

from app.observability import get_trace_recorder
from app.retrieval.fusion import HybridRetriever
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


def test_hybrid_retriever_preserves_legacy_search_contract():
    class Service:
        def search(self, query, top_k):
            return {"products": [{"id": 1}], "rag_results": [{"content": "评价"}], "top_k": top_k}

    hybrid = HybridRetriever.__new__(HybridRetriever)
    hybrid.service = Service()

    result = hybrid.search("跑鞋", top_k=3)

    assert result["products"] == [{"id": 1}]
    assert result["rag_results"] == [{"content": "评价"}]
    assert result["top_k"] == 3


def test_legacy_search_promotes_rag_documents_when_sql_has_no_products():
    class EmptySql:
        def query(self, query):
            return {"sql": "", "results": [], "needs_rag": [], "count": 0}

    class OneDocRag:
        def retrieve(self, query, k):
            return RetrievalResult(documents=[{
                "content": "真实评价",
                "metadata": {"source_type": "reviews"},
                "score": 0.1,
            }])

    service = RetrievalService(EmptySql(), OneDocRag(), reranker=None)

    result = service.search("评价", top_k=1)

    assert result["products"] == [{
        "rag_content": "真实评价",
        "metadata": {"source_type": "reviews"},
    }]


def test_retrieval_cache_hits_are_isolated_by_owner_strategy_and_needs():
    sql = _SqlSpy()
    rag = _RagSpy()
    service = RetrievalService(sql, rag, reranker=None, cache_ttl_seconds=60)

    first = service.retrieve(
        "推荐跑鞋",
        strategy="mixed",
        needs={"needs_product": True},
        owner="user-a",
    )
    first["products"][0]["name"] = "调用方修改"
    second = service.retrieve(
        "推荐跑鞋",
        strategy="mixed",
        needs={"needs_product": True},
        owner="user-a",
    )

    assert second["products"][0]["name"] == "跑鞋"
    assert len(sql.calls) == 1
    assert service.cache_stats()["hits"] == 1

    service.retrieve(
        "推荐跑鞋",
        strategy="mixed",
        needs={"needs_product": True},
        owner="user-b",
    )
    service.retrieve(
        "推荐跑鞋",
        strategy="rag_only",
        needs={"needs_review": True},
        owner="user-a",
    )
    service.retrieve(
        "推荐跑鞋",
        strategy="mixed",
        needs={"needs_product": False},
        owner="user-a",
    )
    assert service.cache_stats()["misses"] == 4


def test_retrieval_failures_are_not_cached():
    class BrokenSql:
        def __init__(self):
            self.calls = 0

        def query(self, query):
            self.calls += 1
            raise RuntimeError("database unavailable")

    service = RetrievalService(
        BrokenSql(),
        _RagSpy(),
        reranker=None,
        cache_ttl_seconds=60,
    )

    first = service.retrieve("跑鞋", strategy="mixed")
    second = service.retrieve("跑鞋", strategy="mixed")

    assert first["sql_error"]
    assert second["sql_error"]
    assert service.nl2sql.calls == 2
    assert service.cache_stats()["entries"] == 0


def test_retrieval_records_sql_rag_total_and_cache_hit_spans():
    recorder = get_trace_recorder()
    recorder.clear()
    service = RetrievalService(_SqlSpy(), _RagSpy(), reranker=None, cache_ttl_seconds=60)

    service.retrieve("推荐跑鞋", strategy="mixed", trace_id="trace-retrieval")
    service.retrieve("推荐跑鞋", strategy="mixed", trace_id="trace-retrieval")

    spans = recorder.summary()["spans"]
    assert spans["retrieval.sql"]["count"] == 1
    assert spans["retrieval.rag"]["count"] == 1
    assert spans["retrieval.total"]["count"] == 1
    assert spans["retrieval.cache_hit"]["count"] == 1


def test_mixed_retrieval_runs_sql_and_base_rag_concurrently():
    barrier = threading.Barrier(2)

    class ConcurrentSql(_SqlSpy):
        def query(self, query):
            barrier.wait(timeout=1)
            return super().query(query)

    class ConcurrentRag(_RagSpy):
        def retrieve(self, query, k):
            barrier.wait(timeout=1)
            return super().retrieve(query, k)

    service = RetrievalService(
        ConcurrentSql(),
        ConcurrentRag(),
        reranker=None,
        cache_ttl_seconds=0,
    )

    result = service.retrieve(
        "推荐跑鞋和评价",
        strategy="mixed",
        concurrent=True,
    )

    assert result["products"]
    assert result["documents"]
    assert result["documents"][0]["content"] == "评价：缓震很好"


def test_concurrent_mixed_retrieval_adds_targeted_rag_only_when_base_is_empty():
    class EmptyThenTargetedRag:
        def __init__(self):
            self.calls = []

        def retrieve(self, query, k):
            self.calls.append(query)
            if len(self.calls) == 1:
                return RetrievalResult(documents=[])
            return RetrievalResult(documents=[{
                "content": "定向评价",
                "metadata": {"product_id": 7},
                "score": 0.2,
            }])

    rag = EmptyThenTargetedRag()
    service = RetrievalService(
        _SqlSpy(),
        rag,
        reranker=None,
        cache_ttl_seconds=0,
    )

    result = service.retrieve(
        "推荐跑鞋",
        strategy="mixed",
        concurrent=True,
    )

    assert rag.calls == ["推荐跑鞋", "推荐跑鞋 评价"]
    assert result["documents"][0]["content"] == "定向评价"


def test_concurrent_source_timeouts_return_partial_results_and_name_each_source():
    class SlowSql:
        def query(self, query):
            time.sleep(0.2)
            return {"results": [], "needs_rag": [], "count": 0}

    class SlowRag:
        def retrieve(self, query, k, owner=None):
            time.sleep(0.2)
            return RetrievalResult(documents=[])

    service = RetrievalService(
        SlowSql(),
        SlowRag(),
        reranker=None,
        cache_ttl_seconds=0,
    )

    started = time.perf_counter()
    result = service.retrieve(
        "推荐跑鞋",
        strategy="mixed",
        concurrent=True,
        source_timeout_seconds=0.02,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15
    assert "sql" in result["source_errors"]
    assert "rag" in result["source_errors"]
    assert "超过 0.02s" in result["source_errors"]["sql"]
    assert "超过 0.02s" in result["source_errors"]["rag"]
    assert result["source_timings_ms"]["sql"] >= 20
    assert result["source_timings_ms"]["rag"] >= 20
    assert result["partial"] is True
