#!/usr/bin/env python3
"""离线运行 Cross-Encoder A/B 评测。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
from app.evaluation.rerank_ab import choose_winner, run_paired_ab
from app.evaluation.rerank_eval import load_cases, rank_keyword


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--salt", default="rerank-v1")
    parser.add_argument("--treatment", choices=("cross_encoder", "keyword"), default="keyword")
    args = parser.parse_args()

    cases = load_cases(args.input)
    treatment_ranker = None
    if args.treatment == "cross_encoder":
        from app.retrieval.reranker import CrossEncoderReranker

        treatment = CrossEncoderReranker()
        if treatment.model is None:
            raise SystemExit("Cross-Encoder 初始化失败")
        treatment_ranker = lambda case: treatment.rerank(
            case.query,
            [dict(document) for document in case.documents],
            top_n=args.k,
        )
    else:
        treatment_ranker = rank_keyword

    result = run_paired_ab(
        cases,
        treatment_ranker=treatment_ranker,
        k=args.k,
        salt=args.salt,
    )
    payload = result.to_dict()
    payload["treatment_strategy"] = args.treatment
    payload["winner"] = choose_winner(result)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
