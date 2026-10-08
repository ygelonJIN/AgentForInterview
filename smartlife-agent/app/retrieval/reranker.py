"""Reranker 模块 - 可选 Cross-Encoder 精排与确定性降级。"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("smartlife")


def _keyword_score(query: str, content: str) -> float:
    query_terms = [term for term in query.lower().split() if term]
    if not query_terms:
        return 0.0
    content_lower = content.lower()
    return sum(1 for term in query_terms if term in content_lower) / len(query_terms)


def _fallback_rank(
    query: str,
    documents: List[Dict[str, Any]],
    top_n: int,
) -> List[Dict[str, Any]]:
    ranked = []
    for index, document in enumerate(documents):
        enriched = dict(document)
        keyword_score = _keyword_score(query, str(document.get("content", "")))
        vector_score = document.get("score")
        enriched["rerank_score"] = keyword_score
        enriched["rerank_source"] = (
            "vector_score_keyword_tiebreak"
            if isinstance(vector_score, (int, float))
            else "keyword"
        )
        ranked.append((enriched, index))

    has_vector_scores = any(
        isinstance(item[0].get("score"), (int, float)) for item in ranked
    )
    if has_vector_scores:
        ranked.sort(
            key=lambda item: (
                float(item[0].get("score", float("inf"))),
                -float(item[0].get("rerank_score", 0.0)),
                item[1],
            )
        )
    else:
        ranked.sort(
            key=lambda item: (-float(item[0].get("rerank_score", 0.0)), item[1])
        )
    return [document for document, _ in ranked[:top_n]]


class CrossEncoderReranker:
    """Cross-Encoder 精排器；模型失败时保留向量排序。"""

    DEFAULT_MODEL = "BAAI/bge-reranker-base"

    def __init__(
        self,
        top_n: int = 5,
        model_name: Optional[str] = None,
        initialize: bool = True,
    ):
        if top_n <= 0:
            raise ValueError("top_n 必须大于 0")
        self.top_n = top_n
        self.model_name = model_name or self.DEFAULT_MODEL
        self.model = None
        if initialize:
            self._initialize()

    def _initialize(self):
        try:
            from sentence_transformers import CrossEncoder

            self.model = CrossEncoder(self.model_name)
        except Exception as exc:
            logger.warning(
                "Cross-Encoder 初始化失败，使用向量/关键词降级: %s",
                exc,
            )
            self.model = None

    def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_n: int = None,
    ) -> List[Dict[str, Any]]:
        if not documents:
            return []
        top_n = self.top_n if top_n is None else top_n
        if top_n <= 0:
            raise ValueError("top_n 必须大于 0")

        if getattr(self, "model", None) is not None:
            try:
                pairs = [
                    (query, str(document.get("content", "")))
                    for document in documents
                ]
                scores = self.model.predict(pairs)
                ranked = []
                for index, (document, score) in enumerate(zip(documents, scores)):
                    enriched = dict(document)
                    enriched["rerank_score"] = float(score)
                    enriched["rerank_source"] = "cross_encoder"
                    ranked.append((enriched, index))
                ranked.sort(
                    key=lambda item: (-item[0]["rerank_score"], item[1])
                )
                return [document for document, _ in ranked[:top_n]]
            except Exception as exc:
                logger.warning("Cross-Encoder 精排失败，保留向量排序: %s", exc)

        return _fallback_rank(query, documents, top_n)


def create_reranker() -> Optional[CrossEncoderReranker]:
    """按环境变量显式启用 Cross-Encoder，默认避免隐式下载模型。"""
    mode = os.environ.get("SMARTLIFE_RERANKER", "none").strip().lower()
    if mode in {"", "none", "off", "disabled"}:
        return None
    if mode in {"cross_encoder", "cross-encoder", "bge"}:
        model_name = os.environ.get("SMARTLIFE_RERANKER_MODEL") or None
        return CrossEncoderReranker(model_name=model_name)
    raise ValueError(f"不支持的 SMARTLIFE_RERANKER: {mode}")
