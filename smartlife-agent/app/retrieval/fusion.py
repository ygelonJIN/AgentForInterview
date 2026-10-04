"""
Fusion 模块 - 混合检索结果融合（增强稳健性）
"""
from typing import List, Dict, Any, Optional

class HybridRetriever:
    """混合检索器 - NL2SQL + RAG + Rerank"""

    def __init__(self, nl2sql_chain=None, rag_retriever=None, reranker=None):
        from app.retrieval.nl2sql import NL2SQLChain
        from app.retrieval.rag import RAGRetriever
        from app.retrieval.reranker import CrossEncoderReranker

        self.nl2sql = nl2sql_chain or NL2SQLChain()
        self.rag = rag_retriever or RAGRetriever()
        self.reranker = reranker or CrossEncoderReranker()

    def _rerank_documents(self, query: str, documents: list, top_n: int = 5) -> list:
        if not documents:
            return []
        try:
            return self.reranker.rerank(query, documents, top_n=top_n)
        except Exception:
            for doc in documents:
                if isinstance(doc, dict) and 'content' in doc:
                    q = (query or '').lower()
                    content = doc['content'].lower()
                    doc.setdefault('rerank_score', sum(1 for word in q.split() if word in content))
            documents.sort(key=lambda x: x.get('rerank_score', 0), reverse=True)
            return documents[:top_n]

    def search(self, user_query: str, top_k: int = 5) -> Dict[str, Any]:
        """混合检索，RAG/Rerank 失败时不再整体抛异常"""
        sql_result: Dict[str, Any] = {}
        rag_ok = True
        rag_error: Optional[str] = None
        rag_results: List[Dict[str, Any]] = []

        try:
            sql_result = self.nl2sql.query(user_query)
        except Exception as e:
            sql_result = {"error": str(e), "sql": None, "needs_rag": [], "results": [], "count": 0}

        rag_query = user_query
        if sql_result.get("needs_rag"):
            rag_query = user_query + " " + " ".join(sql_result["needs_rag"])

        try:
            rag_results = self.rag.search(rag_query, k=20)
        except Exception as e:
            rag_ok = False
            rag_error = f"{type(e).__name__}: {str(e)[:300]}"
            rag_results = []

        products = sql_result.get("results", [])

        if rag_results and products:
            rag_by_product: Dict[Any, List[str]] = {}
            for doc in rag_results:
                metadata = doc.get("metadata", {})
                pid = metadata.get("product_id")
                if pid:
                    rag_by_product.setdefault(pid, []).append(doc.get("content", ""))

            for product in products:
                pid = product.get("id")
                if pid in rag_by_product:
                    product["related_reviews"] = rag_by_product[pid][:3]

        if products:
            product_docs = []
            for p in products:
                content = f"{p.get('name', '')} {p.get('description', '')} {' '.join(p.get('related_reviews', []))}"
                product_docs.append({"content": content, "product": p})
            reranked = self._rerank_documents(user_query, product_docs, top_n=top_k)
            products = [doc["product"] for doc in reranked]
        elif rag_results:
            reranked = self._rerank_documents(user_query, rag_results, top_n=top_k)
            products = [{"rag_content": doc["content"], "metadata": doc.get("metadata", {})} for doc in reranked]

        return {
            "sql": sql_result.get("sql"),
            "explanation": sql_result.get("explanation"),
            "products": products[:top_k],
            "rag_results": rag_results[:top_k],
            "total_sql_results": sql_result.get("count", 0),
            "total_rag_results": len(rag_results),
            "rag_ok": rag_ok,
            "rag_error": rag_error,
            "sql_error": sql_result.get("error"),
        }
