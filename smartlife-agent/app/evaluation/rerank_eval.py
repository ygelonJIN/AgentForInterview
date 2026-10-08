"""Rerank 离线评测：比较向量、关键词和 Cross-Encoder 排序质量。"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class RerankEvalError(ValueError):
    """评测数据格式错误。"""


@dataclass(frozen=True)
class RankingCase:
    query: str
    documents: Tuple[Dict[str, Any], ...]
    relevant_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RankingMetrics:
    recall_at_k: float
    mrr_at_k: float
    ndcg_at_k: float
    relevant_count: int
    retrieved_relevant_count: int


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RerankEvalError(f"{field} 必须是非空字符串")
    return value.strip()


def parse_case(payload: Any) -> RankingCase:
    if not isinstance(payload, dict):
        raise RerankEvalError("每条评测数据必须是 JSON 对象")
    query = _required_string(payload.get("query"), "query")
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise RerankEvalError("documents 必须是非空列表")

    documents: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, raw_document in enumerate(raw_documents):
        if not isinstance(raw_document, dict):
            raise RerankEvalError(f"documents[{index}] 必须是对象")
        document_id = _required_string(
            raw_document.get("id", f"doc-{index + 1}"),
            f"documents[{index}].id",
        )
        if document_id in seen_ids:
            raise RerankEvalError(f"documents id 重复: {document_id}")
        seen_ids.add(document_id)
        content = _required_string(
            raw_document.get("content"),
            f"documents[{index}].content",
        )
        score = raw_document.get("score")
        if score is not None and not isinstance(score, (int, float)):
            raise RerankEvalError(f"documents[{index}].score 必须是数字")
        documents.append({
            "id": document_id,
            "content": content,
            "score": float(score) if score is not None else None,
            "metadata": dict(raw_document.get("metadata") or {}),
        })

    raw_relevant = payload.get("relevant_ids")
    if not isinstance(raw_relevant, list) or not raw_relevant:
        raise RerankEvalError("relevant_ids 必须是非空列表")
    relevant_ids = tuple(
        _required_string(item, "relevant_ids item") for item in raw_relevant
    )
    unknown_ids = sorted(set(relevant_ids) - seen_ids)
    if unknown_ids:
        raise RerankEvalError("relevant_ids 引用了不存在的文档: " + ", ".join(unknown_ids))
    return RankingCase(
        query=query,
        documents=tuple(documents),
        relevant_ids=tuple(dict.fromkeys(relevant_ids)),
    )


def load_cases(path: Path) -> List[RankingCase]:
    cases = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                cases.append(parse_case(payload))
            except (json.JSONDecodeError, RerankEvalError) as exc:
                raise RerankEvalError(f"{path}:{line_number}: {exc}") from exc
    if not cases:
        raise RerankEvalError("评测文件没有任何有效案例")
    return cases


def _keyword_terms(query: str) -> List[str]:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", query.casefold()).strip()
    terms = [term for term in normalized.split() if term]
    for token in tuple(terms):
        if re.search(r"[\u4e00-\u9fff]", token):
            terms.extend(token[index:index + 2] for index in range(len(token) - 1))
    return list(dict.fromkeys(term for term in terms if term))


def _keyword_score(query: str, content: str) -> float:
    terms = _keyword_terms(query)
    if not terms:
        return 0.0
    content_lower = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", content.casefold())
    return sum(term in content_lower for term in terms) / len(terms)


def rank_vector(case: RankingCase) -> List[Dict[str, Any]]:
    return [
        dict(document)
        for document in sorted(
            case.documents,
            key=lambda item: (
                item["score"] if item["score"] is not None else float("inf"),
                item["id"],
            ),
        )
    ]


def rank_keyword(case: RankingCase) -> List[Dict[str, Any]]:
    return [
        dict(document)
        for document in sorted(
            case.documents,
            key=lambda item: (
                -_keyword_score(case.query, item["content"]),
                item["score"] if item["score"] is not None else float("inf"),
                item["id"],
            ),
        )
    ]


def rank_cross_encoder(
    case: RankingCase,
    reranker: Any,
) -> List[Dict[str, Any]]:
    ranked = reranker.rerank(case.query, list(case.documents), top_n=len(case.documents))
    if not isinstance(ranked, list):
        raise RerankEvalError("Cross-Encoder reranker 必须返回列表")
    return [dict(document) for document in ranked]


def calculate_metrics(
    ranked: Sequence[Dict[str, Any]],
    relevant_ids: Sequence[str],
    k: int,
) -> RankingMetrics:
    if k <= 0:
        raise RerankEvalError("k 必须大于 0")
    relevant = set(relevant_ids)
    top = list(ranked[:k])
    retrieved_relevant = [item for item in top if item.get("id") in relevant]

    recall = len(retrieved_relevant) / len(relevant)
    mrr = 0.0
    for rank, item in enumerate(top, start=1):
        if item.get("id") in relevant:
            mrr = 1.0 / rank
            break

    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, item in enumerate(top, start=1)
        if item.get("id") in relevant
    )
    ideal_count = min(len(relevant), k)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    ndcg = dcg / ideal_dcg if ideal_dcg else 0.0

    return RankingMetrics(
        recall_at_k=recall,
        mrr_at_k=mrr,
        ndcg_at_k=ndcg,
        relevant_count=len(relevant),
        retrieved_relevant_count=len(retrieved_relevant),
    )


def evaluate_strategy(
    cases: Sequence[RankingCase],
    ranker,
    k: int,
) -> Dict[str, Any]:
    metrics = [
        calculate_metrics(ranker(case), case.relevant_ids, k)
        for case in cases
    ]
    count = len(metrics)
    return {
        "case_count": count,
        f"recall@{k}": sum(item.recall_at_k for item in metrics) / count,
        f"mrr@{k}": sum(item.mrr_at_k for item in metrics) / count,
        f"ndcg@{k}": sum(item.ndcg_at_k for item in metrics) / count,
    }


def evaluate_rerank_cases(
    cases: Sequence[RankingCase],
    k: int = 5,
    cross_encoder_reranker: Optional[Any] = None,
) -> Dict[str, Any]:
    if not cases:
        raise RerankEvalError("cases 不能为空")
    results = {
        "vector": evaluate_strategy(cases, rank_vector, k),
        "keyword": evaluate_strategy(cases, rank_keyword, k),
    }
    if cross_encoder_reranker is not None:
        results["cross_encoder"] = evaluate_strategy(
            cases,
            lambda case: rank_cross_encoder(case, cross_encoder_reranker),
            k,
        )
    return {
        "k": k,
        "case_count": len(cases),
        "strategies": results,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rerank Recall/MRR/NDCG 离线评测")
    parser.add_argument("--input", required=True, type=Path, help="JSONL 评测集")
    parser.add_argument("--k", type=int, default=5, help="截断位置，默认 5")
    parser.add_argument("--output", type=Path, help="输出 JSON 文件；不传则打印")
    parser.add_argument(
        "--cross-encoder",
        action="store_true",
        help="启用 Cross-Encoder；需要本地模型和 sentence-transformers",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cases = load_cases(args.input)
    cross_encoder = None
    if args.cross_encoder:
        from app.retrieval.reranker import CrossEncoderReranker

        cross_encoder = CrossEncoderReranker()
        if cross_encoder.model is None:
            raise RerankEvalError("Cross-Encoder 初始化失败，无法执行显式评测")
    result = evaluate_rerank_cases(
        cases,
        k=args.k,
        cross_encoder_reranker=cross_encoder,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
