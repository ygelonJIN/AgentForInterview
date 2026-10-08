#!/usr/bin/env python3
"""从在线曝光/点击记录导出可回放 Rerank 评测集。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--experiment", default="rerank-v1")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-clicks", type=int, default=1)
    args = parser.parse_args()

    with sqlite3.connect(args.db) as connection:
        impressions = connection.execute(
            """
            SELECT query_hash, query_text, metadata_json
            FROM ranking_events
            WHERE experiment = ? AND event_type = 'impression'
            ORDER BY created_at
            """,
            (args.experiment,),
        ).fetchall()
        clicks = connection.execute(
            """
            SELECT query_hash, document_id
            FROM ranking_events
            WHERE experiment = ? AND event_type IN ('click', 'conversion')
            """,
            (args.experiment,),
        ).fetchall()

    clicked: dict[str, set[str]] = {}
    for query_hash, document_id in clicks:
        if document_id:
            clicked.setdefault(query_hash, set()).add(document_id)

    cases = []
    seen = set()
    for query_hash, query_text, metadata_json in impressions:
        if query_hash in seen:
            continue
        seen.add(query_hash)
        metadata = json.loads(metadata_json or "{}")
        document_ids = list(metadata.get("ranked_document_ids") or [])
        known_documents = set(document_ids)
        relevant = clicked.get(query_hash, set()) & known_documents
        if len(relevant) < args.min_clicks:
            continue
        documents = [{"id": item} for item in document_ids]
        if not documents:
            continue
        cases.append({
            "query": query_text,
            "documents": documents,
            "relevant_ids": sorted(relevant),
        })

    if not cases:
        raise SystemExit("没有满足条件的在线业务案例")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
