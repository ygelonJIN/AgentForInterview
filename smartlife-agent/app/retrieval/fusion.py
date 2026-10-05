"""
[COMPATIBILITY] 旧 HybridRetriever facade。

当前 V2 使用 `app.retrieval.service.RetrievalService`。
"""
from typing import List, Dict, Any, Optional

class HybridRetriever:
    """混合检索器 - NL2SQL + RAG + Rerank"""

    def __init__(self, nl2sql_chain=None, rag_retriever=None, reranker=None):
        from app.retrieval.reranker import CrossEncoderReranker
        from app.retrieval.service import RetrievalService

        self.service = RetrievalService(
            nl2sql_chain=nl2sql_chain,
            rag_retriever=rag_retriever,
            reranker=reranker or CrossEncoderReranker(),
        )

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
        """兼容旧接口，统一委托 RetrievalService。"""
        return self.service.search(user_query, top_k=top_k)
