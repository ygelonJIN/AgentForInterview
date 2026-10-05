"""统一 RetrievalService 契约测试。"""

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

    def retrieve(self, query, k):
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
