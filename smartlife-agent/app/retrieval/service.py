"""
统一检索服务。

SQL、RAG 和 Rerank 的调用、融合、降级与诊断集中在这里，编排器只负责
把结果转成提示词上下文和 UI 事件。
"""
import copy
import json
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    wait,
)
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.observability import record_trace
from app.retrieval.planning import build_retrieval_plan


class RetrievalService:
    """统一的 SQL + RAG + Rerank 检索入口。"""

    def __init__(
        self,
        nl2sql_chain=None,
        rag_retriever=None,
        reranker=None,
        *,
        cache_ttl_seconds: float = 60.0,
        cache_max_entries: int = 128,
    ):
        if nl2sql_chain is None or rag_retriever is None:
            from app.retrieval.nl2sql import NL2SQLChain
            from app.retrieval.rag import RAGRetriever
            nl2sql_chain = nl2sql_chain or NL2SQLChain()
            rag_retriever = rag_retriever or RAGRetriever()
        self.nl2sql = nl2sql_chain
        self.rag = rag_retriever
        self.reranker = reranker
        self.cache_ttl_seconds = cache_ttl_seconds
        self.cache_max_entries = cache_max_entries
        self._cache: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
        self._cache_lock = threading.RLock()
        self._cache_stats = {"hits": 0, "misses": 0, "evictions": 0}

    def _cache_key(
        self,
        query: str,
        strategy: str,
        top_k: int,
        needs: Optional[Dict[str, Any]],
        owner: Optional[str],
        plan: Optional[Dict[str, Any]] = None,
    ) -> str:
        normalized_query = re.sub(r"\s+", " ", query or "").strip().casefold()
        payload = {
            "query": normalized_query,
            "strategy": strategy,
            "top_k": top_k,
            "needs": needs or {},
            "owner": owner,
            "plan": plan or {},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        if self.cache_ttl_seconds <= 0 or self.cache_max_entries <= 0:
            return None
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is None:
                self._cache_stats["misses"] += 1
                return None
            created_at, result = cached
            if now - created_at >= self.cache_ttl_seconds:
                self._cache.pop(key, None)
                self._cache_stats["misses"] += 1
                return None
            self._cache.move_to_end(key)
            self._cache_stats["hits"] += 1
            return copy.deepcopy(result)

    def _cache_put(self, key: str, result: Dict[str, Any]) -> None:
        if self.cache_ttl_seconds <= 0 or self.cache_max_entries <= 0:
            return
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), copy.deepcopy(result))
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max_entries:
                self._cache.popitem(last=False)
                self._cache_stats["evictions"] += 1

    def cache_stats(self) -> Dict[str, int]:
        with self._cache_lock:
            return {**self._cache_stats, "entries": len(self._cache)}

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

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

    def _query_sql(self, query: str, trace_id: str = "") -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            result = self.nl2sql.query(query)
            if not isinstance(result, dict):
                raise TypeError("NL2SQL 返回结果必须是字典")
            if not isinstance(result.get("results"), list):
                result["results"] = []
            result.setdefault("needs_rag", [])
            result.setdefault("count", len(result["results"]))
            record_trace(
                "retrieval.sql",
                (time.perf_counter() - started) * 1000,
                trace_id=trace_id,
                status="error" if result.get("error") else "ok",
                attributes={"result_count": result.get("count", 0)},
            )
            return result
        except Exception as exc:
            result = {
                "sql": "",
                "explanation": "",
                "needs_rag": [],
                "results": [],
                "count": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
            record_trace(
                "retrieval.sql",
                (time.perf_counter() - started) * 1000,
                trace_id=trace_id,
                status="error",
                attributes={"error": result["error"]},
            )
            return result

    def _query_rag(
        self,
        query: str,
        k: int = 20,
        owner: Optional[str] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            kwargs = {"k": k}
            if owner is not None:
                kwargs["owner"] = owner
            if hasattr(self.rag, "retrieve"):
                raw = self.rag.retrieve(query, **kwargs)
                result = raw.to_dict() if hasattr(raw, "to_dict") else raw
            else:
                result = {
                    "documents": self.rag.search(query, **kwargs),
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
            record_trace(
                "retrieval.rag",
                (time.perf_counter() - started) * 1000,
                trace_id=trace_id,
                status="ok" if result.get("ok") else "error",
                attributes={
                    "document_count": len(result.get("documents") or []),
                    "owner_scoped": owner is not None,
                },
            )
            return result
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            result = {
                "documents": [],
                "scores": [],
                "diagnostics": [f"RAG 检索失败: {message}"],
                "source": "rag",
                "ok": False,
                "error": message,
            }
            record_trace(
                "retrieval.rag",
                (time.perf_counter() - started) * 1000,
                trace_id=trace_id,
                status="error",
                attributes={"error": message},
            )
            return result

    def _rerank_with_mode(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_n: int,
        use_reranker: bool = True,
    ) -> Tuple[List[Dict[str, Any]], str]:
        if not documents:
            return [], "none"
        if use_reranker and self.reranker is not None:
            try:
                ranked = self.reranker.rerank(query, documents, top_n=top_n)
                if isinstance(ranked, list):
                    mode = str(
                        ranked[0].get("rerank_source", "cross_encoder")
                        if ranked
                        else "cross_encoder"
                    )
                    return ranked, mode
            except Exception:
                pass

        # 精排失败时优先保留原始向量顺序，再用关键词打破同分。
        query_terms = [term for term in query.lower().split() if term]
        ranked = []
        for index, document in enumerate(documents):
            content = document.get("content", "").lower()
            keyword_score = sum(1 for term in query_terms if term in content)
            enriched = dict(document)
            enriched["rerank_score"] = keyword_score / max(len(query_terms), 1)
            ranked.append((enriched, index))

        has_vector_scores = any(
            isinstance(item[0].get("score"), (int, float)) for item in ranked
        )
        if has_vector_scores:
            ranked.sort(key=lambda item: (
                float(item[0].get("score", float("inf"))),
                -float(item[0].get("rerank_score", 0.0)),
                item[1],
            ))
            mode = "vector_score_keyword_tiebreak"
        else:
            ranked.sort(key=lambda item: (
                -float(item[0].get("rerank_score", 0.0)),
                item[1],
            ))
            mode = "keyword"
        for document, _ in ranked:
            document["rerank_source"] = mode
        return [document for document, _ in ranked[:top_n]], mode

    def _rerank(self, query: str, documents: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
        ranked, _ = self._rerank_with_mode(query, documents, top_n)
        return ranked

    @staticmethod
    def _source_timeout_result(
        source: str,
        timeout_seconds: float,
        started: float,
    ) -> Dict[str, Any]:
        message = f"{source} 数据源超过 {timeout_seconds:.3g}s 未返回"
        record_trace(
            f"retrieval.{source}",
            (time.perf_counter() - started) * 1000,
            trace_id="",
            status="error",
            attributes={"error": message, "timeout_seconds": timeout_seconds},
        )
        if source == "sql":
            return {
                "sql": "",
                "explanation": "",
                "needs_rag": [],
                "results": [],
                "count": 0,
                "error": message,
            }
        return {
            "documents": [],
            "scores": [],
            "diagnostics": [f"RAG 检索超时: {message}"],
            "source": "rag",
            "ok": False,
            "error": message,
        }

    @staticmethod
    def _timed_source(
        timings: Dict[str, float],
        label: str,
        operation: Callable[[], Dict[str, Any]],
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            return operation()
        finally:
            timings[label] = round((time.perf_counter() - started) * 1000, 3)

    @classmethod
    def _collect_source_result(
        cls,
        source: str,
        future: "Future[Dict[str, Any]]",
        timeout_seconds: float,
        started: float,
        timings: Optional[Dict[str, float]] = None,
        timing_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            result = future.result(timeout=timeout_seconds)
            if not isinstance(result, dict):
                raise TypeError(f"{source} 数据源返回结果必须是字典")
            return result
        except FutureTimeoutError:
            future.cancel()
            if timings is not None:
                timings[timing_label or source] = round(timeout_seconds * 1000, 3)
            return cls._source_timeout_result(source, timeout_seconds, started)
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            return cls._source_timeout_result(source, timeout_seconds, started) | {
                "error": message,
                "diagnostics": [f"{source} 数据源失败: {message}"],
            }

    def _run_single_source(
        self,
        source: str,
        operation: Callable[[], Dict[str, Any]],
        timeout_seconds: float,
        timings: Optional[Dict[str, float]] = None,
        timing_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        label = timing_label or source
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"retrieval-{source}")
        future = executor.submit(self._timed_source, timings or {}, label, operation)
        try:
            return self._collect_source_result(
                source,
                future,
                timeout_seconds,
                started,
                timings=timings,
                timing_label=label,
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def retrieve(
        self,
        query: str,
        strategy: str = "mixed",
        top_k: int = 5,
        needs: Optional[Dict[str, Any]] = None,
        owner: Optional[str] = None,
        concurrent: bool = False,
        trace_id: str = "",
        rerank_variant: Optional[str] = None,
        source_timeout_seconds: Optional[float] = 20.0,
    ) -> Dict[str, Any]:
        """按证据计划执行检索；不同数据源不是主备降级关系。

        每个数据源使用独立截止时间。超时时保留其他数据源的可用证据，并明确记录
        是 SQL、RAG 还是定向 RAG 超时，避免外层总超时掩盖真正的阻塞点。
        """
        plan = build_retrieval_plan(strategy=strategy, needs=needs)
        if rerank_variant not in {None, "control", "treatment"}:
            raise ValueError("rerank_variant 必须是 control、treatment 或 None")
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        if source_timeout_seconds is not None and source_timeout_seconds <= 0:
            raise ValueError("source_timeout_seconds 必须大于 0")
        cache_key = self._cache_key(
            query,
            strategy,
            top_k,
            needs,
            owner,
            plan.to_dict(),
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            record_trace(
                "retrieval.cache_hit",
                0.0,
                trace_id=trace_id,
                attributes={"strategy": strategy},
            )
            return cached

        retrieval_started = time.perf_counter()
        diagnostics: List[str] = []
        sql_result: Dict[str, Any] = {}
        rag_result: Dict[str, Any] = {
            "documents": [],
            "scores": [],
            "diagnostics": [],
            "ok": None,
            "error": None,
        }

        rag_k = max(top_k * 4, 20)
        selected_sources = set(plan.selected_sources)
        source_timings: Dict[str, float] = {}
        effective_source_timeout = float(source_timeout_seconds or 20.0)
        if selected_sources == {"sql", "rag"} and concurrent:
            executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="retrieval-mixed")
            started = time.perf_counter()
            futures = {
                "sql": executor.submit(
                    self._timed_source,
                    source_timings,
                    "sql",
                    lambda: self._query_sql(query, trace_id),
                ),
                "rag": executor.submit(
                    self._timed_source,
                    source_timings,
                    "rag",
                    lambda: self._query_rag(
                        query,
                        k=rag_k,
                        owner=owner,
                        trace_id=trace_id,
                    ),
                ),
            }
            wait(list(futures.values()), timeout=effective_source_timeout)
            sql_result = self._collect_source_result(
                "sql",
                futures["sql"],
                effective_source_timeout,
                started,
                timings=source_timings,
                timing_label="sql",
            )
            rag_result = self._collect_source_result(
                "rag",
                futures["rag"],
                effective_source_timeout,
                started,
                timings=source_timings,
                timing_label="rag",
            )
            executor.shutdown(wait=False, cancel_futures=True)

            # SQL 的语义条件只在基础 RAG 证据不足时补查，避免无条件增加一次 Embedding。
            if sql_result.get("needs_rag") and (
                not rag_result.get("ok") or len(rag_result.get("documents") or []) < 2
            ):
                targeted_query = (
                    f"{query} {' '.join(sql_result['needs_rag'])}".strip()
                )
                targeted_rag = self._run_single_source(
                    "rag",
                    lambda: self._query_rag(
                        targeted_query,
                        k=rag_k,
                        owner=owner,
                        trace_id=trace_id,
                    ),
                    effective_source_timeout,
                    timings=source_timings,
                    timing_label="rag_targeted",
                )
                seen = {
                    json.dumps(
                        {
                            "content": item.get("content", ""),
                            "metadata": item.get("metadata", {}),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    for item in rag_result.get("documents") or []
                }
                merged_documents = list(rag_result.get("documents") or [])
                for item in targeted_rag.get("documents") or []:
                    identity = json.dumps(
                        {
                            "content": item.get("content", ""),
                            "metadata": item.get("metadata", {}),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if identity not in seen:
                        merged_documents.append(item)
                        seen.add(identity)
                rag_result = {
                    **targeted_rag,
                    "documents": merged_documents,
                    "ok": bool(rag_result.get("ok")) or bool(targeted_rag.get("ok")),
                    "diagnostics": list(
                        dict.fromkeys(
                            (rag_result.get("diagnostics") or [])
                            + (targeted_rag.get("diagnostics") or [])
                        )
                    ),
                }
        else:
            if "sql" in selected_sources:
                sql_result = self._run_single_source(
                    "sql",
                    lambda: self._query_sql(query, trace_id),
                    effective_source_timeout,
                    timings=source_timings,
                    timing_label="sql",
                )
            rag_query = query
            if sql_result.get("needs_rag"):
                rag_query = f"{query} {' '.join(sql_result['needs_rag'])}".strip()
            if "rag" in selected_sources:
                rag_result = self._run_single_source(
                    "rag",
                    lambda: self._query_rag(
                        rag_query,
                        k=rag_k,
                        owner=owner,
                        trace_id=trace_id,
                    ),
                    effective_source_timeout,
                    timings=source_timings,
                    timing_label="rag",
                )

        if "sql" in selected_sources:
            sql_failure_stage = str(sql_result.get("failure_stage") or "")
            if sql_result.get("error"):
                stage_label = {
                    "nl2sql_model": "小模型生成 SQL 失败",
                    "sql_execution": "SQLite 执行 SQL 失败",
                }.get(sql_failure_stage, "NL2SQL 错误")
                diagnostics.append(f"{stage_label}: {sql_result['error']}")
            elif not sql_result.get("results"):
                diagnostics.append("权威 SQL 查询未返回商品结果")
        if "rag" in selected_sources:
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

        product_rerank_mode = "none"
        document_rerank_mode = "none"
        if products:
            product_documents = [{
                "content": " ".join([
                    str(product.get("name", "")),
                    str(product.get("description", "")),
                    " ".join(product.get("related_reviews", [])),
                ]),
                "product": product,
            } for product in products]
            ranked_products, product_rerank_mode = self._rerank_with_mode(
                query,
                product_documents,
                top_k,
                use_reranker=rerank_variant != "control",
            )
            products = [item["product"] for item in ranked_products]
        else:
            products = []

        if documents and "rag" in selected_sources:
            documents, document_rerank_mode = self._rerank_with_mode(
                query,
                documents,
                top_k,
                use_reranker=rerank_variant != "control",
            )
        else:
            documents = documents[:top_k]

        source_errors: Dict[str, str] = {}
        empty_sources = []
        if "sql" in selected_sources:
            if sql_result.get("error"):
                source_errors["sql"] = str(sql_result["error"])
            elif not products:
                empty_sources.append("sql")
        if "rag" in selected_sources:
            if rag_result.get("error") or rag_result.get("ok") is False:
                source_errors["rag"] = str(rag_result.get("error") or "RAG 检索失败")
            elif not documents:
                empty_sources.append("rag")
        failed_required = sorted(set(plan.required_sources) & set(source_errors))
        required_count = len(plan.required_sources)
        evidence_coverage = (
            1.0
            if not required_count
            else round((required_count - len(failed_required)) / required_count, 4)
        )
        result = {
            "strategy": strategy,
            "effective_strategy": plan.derived_strategy,
            "plan": plan.to_dict(),
            "selected_sources": list(plan.selected_sources),
            "source_status": {
                source: ("error" if source in source_errors else "ok" if source in selected_sources else "not_selected")
                for source in ("sql", "rag", "tool", "memory")
            },
            "source_errors": source_errors,
            "source_timings_ms": source_timings,
            "source_timeout_seconds": effective_source_timeout,
            "empty_sources": empty_sources,
            "warnings": list(sql_result.get("warnings") or []),
            "sql_authoritative": sql_result.get("authoritative"),
            "evidence_coverage": evidence_coverage,
            "missing_evidence": failed_required,
            "partial": bool(failed_required) and plan.allow_partial,
            "unanswerable": bool(failed_required) and not plan.allow_partial,
            "needs": needs or {},
            "sql": sql_result.get("sql", ""),
            "explanation": sql_result.get("explanation", ""),
            "products": products[:top_k],
            "documents": documents[:top_k],
            "rag_results": documents[:top_k],
            "diagnostics": diagnostics,
            "sql_ok": not bool(sql_result.get("error")) if "sql" in selected_sources else None,
            "rag_ok": rag_result.get("ok") if "rag" in selected_sources else None,
            "rag_error": rag_result.get("error") if "rag" in selected_sources else None,
            "sql_error": sql_result.get("error") if "sql" in selected_sources else None,
            "sql_failure_stage": sql_result.get("failure_stage", "") if "sql" in selected_sources else "",
            "total_sql_results": sql_result.get("count", 0),
            "total_rag_results": len(rag_result.get("documents", [])),
            "source": {"sql": "sqlite", "rag": rag_result.get("source", "rag")},
            "rerank": {
                "variant": rerank_variant,
                "products": product_rerank_mode,
                "documents": document_rerank_mode,
            },
        }
        if not result.get("sql_error") and not result.get("rag_error"):
            self._cache_put(cache_key, result)
        record_trace(
            "retrieval.total",
            (time.perf_counter() - retrieval_started) * 1000,
            trace_id=trace_id,
            status="error" if result.get("sql_error") or result.get("rag_error") else "ok",
            attributes={
                "strategy": strategy,
                "effective_strategy": plan.derived_strategy,
                "selected_sources": ",".join(plan.selected_sources),
                "evidence_coverage": evidence_coverage,
                "concurrent": concurrent,
                "product_count": len(result.get("products") or []),
                "document_count": len(result.get("documents") or []),
            },
        )
        return result

    def search(
        self,
        user_query: str,
        top_k: int = 5,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """兼容 HybridRetriever.search 的旧返回结构。"""
        result = self.retrieve(user_query, strategy="mixed", top_k=top_k, owner=owner)
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
