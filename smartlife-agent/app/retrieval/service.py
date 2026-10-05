"""
统一检索服务。

SQL、RAG 和 Rerank 的调用、融合、降级与诊断集中在这里，编排器只负责
把结果转成提示词上下文和 UI 事件。
"""
from typing import Any, Dict, List, Optional


class RetrievalService:
    """统一的 SQL + RAG + Rerank 检索入口。"""

    def __init__(self, nl2sql_chain=None, rag_retriever=None, reranker=None):
        if nl2sql_chain is None or rag_retriever is None:
            from app.retrieval.nl2sql import NL2SQLChain
            from app.retrieval.rag import RAGRetriever
            nl2sql_chain = nl2sql_chain or NL2SQLChain()
            rag_retriever = rag_retriever or RAGRetriever()
        self.nl2sql = nl2sql_chain
        self.rag = rag_retriever
        self.reranker = reranker

    @staticmethod
    def _normalize_documents(documents: Any) -> List[Dict[str, Any]]:
        normalized = []
        for document in documents or []:
            if isinstance(document, dict):
                normalized.append({
                    "content": document.get("content", ""),
                    "metadata": dict(document.get("metadata") or {}),
                    "score": document.get("score", 0.0),
                })
            else:
                normalized.append({
                    "content": getattr(document, "page_content", str(document)),
                    "metadata": dict(getattr(document, "metadata", {}) or {}),
                    "score": getattr(document, "score", 0.0),
                })
        return normalized

    def _query_sql(self, query: str) -> Dict[str, Any]:
        try:
            result = self.nl2sql.query(query)
            if not isinstance(result, dict):
                raise TypeError("NL2SQL 返回结果必须是字典")
            if not isinstance(result.get("results"), list):
                result["results"] = []
            result.setdefault("needs_rag", [])
            result.setdefault("count", len(result["results"]))
            return result
        except Exception as exc:
            return {
                "sql": "",
                "explanation": "",
                "needs_rag": [],
                "results": [],
                "count": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }

    def _query_rag(self, query: str, k: int = 20) -> Dict[str, Any]:
        try:
            if hasattr(self.rag, "retrieve"):
                raw = self.rag.retrieve(query, k=k)
                result = raw.to_dict() if hasattr(raw, "to_dict") else raw
            else:
                result = {
                    "documents": self.rag.search(query, k=k),
                    "scores": [],
                    "diagnostics": [],
                    "source": "rag",
                    "ok": True,
                    "error": None,
                }
            if not isinstance(result, dict):
                raise TypeError("RAG 返回结果必须是字典")
            result["documents"] = self._normalize_documents(result.get("documents"))
            result.setdefault("diagnostics", [])
            result.setdefault("ok", True)
            result.setdefault("error", None)
            if result["ok"] is False:
                # 失败结果不得混入半成品文档，避免生成端把它们当真实证据。
                result["documents"] = []
                result["scores"] = []
            return result
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            return {
                "documents": [],
                "scores": [],
                "diagnostics": [f"RAG 检索失败: {message}"],
                "source": "rag",
                "ok": False,
                "error": message,
            }

    def _rerank(self, query: str, documents: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
        if not documents:
            return []
        if self.reranker is not None:
            try:
                return self.reranker.rerank(query, documents, top_n=top_n)
            except Exception:
                pass

        # 无 Cross-Encoder 或精排失败时使用可解释的关键词降级。
        query_terms = [term for term in query.lower().split() if term]
        ranked = []
        for document in documents:
            content = document.get("content", "").lower()
            score = sum(1 for term in query_terms if term in content)
            enriched = dict(document)
            enriched["rerank_score"] = score / max(len(query_terms), 1)
            ranked.append(enriched)
        ranked.sort(key=lambda item: item.get("rerank_score", 0.0), reverse=True)
        return ranked[:top_n]

    def retrieve(
        self,
        query: str,
        strategy: str = "mixed",
        top_k: int = 5,
        needs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """按策略执行检索，并返回统一的结构化结果。"""
        if strategy not in {"sql_only", "rag_only", "mixed"}:
            raise ValueError(f"不支持的检索策略: {strategy}")
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")

        diagnostics: List[str] = []
        sql_result: Dict[str, Any] = {}
        rag_result: Dict[str, Any] = {
            "documents": [],
            "scores": [],
            "diagnostics": [],
            "ok": None,
            "error": None,
        }

        if strategy in {"sql_only", "mixed"}:
            sql_result = self._query_sql(query)
            if sql_result.get("error"):
                diagnostics.append(f"NL2SQL 错误: {sql_result['error']}")
            elif not sql_result.get("results"):
                diagnostics.append("NL2SQL 未返回商品结果")

        rag_query = query
        if sql_result.get("needs_rag"):
            rag_query = f"{query} {' '.join(sql_result['needs_rag'])}".strip()
        if strategy in {"rag_only", "mixed"}:
            rag_result = self._query_rag(rag_query, k=max(top_k * 4, 20))
            diagnostics.extend(rag_result.get("diagnostics") or [])
            if rag_result["ok"] and not rag_result["documents"]:
                diagnostics.append("RAG 未返回评价结果")

        products = [dict(product) for product in sql_result.get("results", [])]
        documents = rag_result.get("documents", [])
        if products and documents:
            reviews_by_product: Dict[Any, List[str]] = {}
            for document in documents:
                product_id = document.get("metadata", {}).get("product_id")
                if product_id is not None:
                    reviews_by_product.setdefault(product_id, []).append(document["content"])
            for product in products:
                product_id = product.get("id")
                if product_id in reviews_by_product:
                    product["related_reviews"] = reviews_by_product[product_id][:3]

        if products:
            product_documents = [{
                "content": " ".join([
                    str(product.get("name", "")),
                    str(product.get("description", "")),
                    " ".join(product.get("related_reviews", [])),
                ]),
                "product": product,
            } for product in products]
            ranked_products = self._rerank(query, product_documents, top_k)
            products = [item["product"] for item in ranked_products]
        else:
            products = []

        if documents and strategy in {"rag_only", "mixed"}:
            documents = self._rerank(query, documents, top_k)
        else:
            documents = documents[:top_k]

        return {
            "strategy": strategy,
            "needs": needs or {},
            "sql": sql_result.get("sql", ""),
            "explanation": sql_result.get("explanation", ""),
            "products": products[:top_k],
            "documents": documents[:top_k],
            "rag_results": documents[:top_k],
            "diagnostics": diagnostics,
            "sql_ok": not bool(sql_result.get("error")) if strategy in {"sql_only", "mixed"} else None,
            "rag_ok": rag_result.get("ok") if strategy in {"rag_only", "mixed"} else None,
            "rag_error": rag_result.get("error") if strategy in {"rag_only", "mixed"} else None,
            "sql_error": sql_result.get("error") if strategy in {"sql_only", "mixed"} else None,
            "total_sql_results": sql_result.get("count", 0),
            "total_rag_results": len(rag_result.get("documents", [])),
            "source": {"sql": "sqlite", "rag": rag_result.get("source", "rag")},
        }

    def search(self, user_query: str, top_k: int = 5) -> Dict[str, Any]:
        """兼容 HybridRetriever.search 的旧返回结构。"""
        result = self.retrieve(user_query, strategy="mixed", top_k=top_k)
        products = result["products"]
        if not products and result["documents"]:
            products = [{
                "rag_content": document["content"],
                "metadata": document.get("metadata", {}),
            } for document in result["documents"]]
        return {
            "sql": result["sql"],
            "explanation": result["explanation"],
            "products": products,
            "rag_results": result["documents"],
            "total_sql_results": result["total_sql_results"],
            "total_rag_results": result["total_rag_results"],
            "rag_ok": result["rag_ok"],
            "rag_error": result["rag_error"],
            "sql_error": result["sql_error"],
        }
