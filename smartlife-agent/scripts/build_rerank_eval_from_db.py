#!/usr/bin/env python3
"""从业务查询日志和当前商品数据库构建 Rerank 评测集。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Set


def load_queries(path: Path) -> List[Dict[str, Any]]:
    queries = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            query = str(payload.get("query") or "").strip()
            relevant = payload.get("relevant_product_ids")
            if not query or not isinstance(relevant, list) or not relevant:
                raise ValueError(f"{path}:{line_number}: 需要 query 和 relevant_product_ids")
            queries.append({
                "query": query,
                "relevant_product_ids": [int(item) for item in relevant],
            })
    if not queries:
        raise ValueError("业务查询日志不能为空")
    return queries


def build_case(
    connection: sqlite3.Connection,
    query: Dict[str, Any],
    *,
    max_documents: int,
) -> Dict[str, Any]:
    relevant_ids: Set[int] = set(query["relevant_product_ids"])
    rows = connection.execute(
        """
        SELECT id, name, category, subcategory, price, waterproof, brand,
               stock, description, rating, created_at
        FROM products
        ORDER BY rating DESC, id ASC
        """
    ).fetchall()
    columns = [
        "id", "name", "category", "subcategory", "price", "waterproof", "brand",
        "stock", "description", "rating", "created_at",
    ]
    products = [dict(zip(columns, row)) for row in rows]
    unknown = sorted(relevant_ids - {int(item["id"]) for item in products})
    if unknown:
        raise ValueError(f"查询引用了不存在的商品 ID: {unknown}")

    selected = [item for item in products if int(item["id"]) in relevant_ids]
    selected += [item for item in products if int(item["id"]) not in relevant_ids]
    selected = selected[:max_documents]

    documents = []
    for product in selected:
        reviews = connection.execute(
            "SELECT content FROM reviews WHERE product_id = ? ORDER BY id LIMIT 3",
            (product["id"],),
        ).fetchall()
        content = " ".join([
            str(product.get("name") or ""),
            str(product.get("category") or ""),
            str(product.get("subcategory") or ""),
            str(product.get("brand") or ""),
            str(product.get("description") or ""),
            *[row[0] for row in reviews],
        ]).strip()
        documents.append({
            "id": f"product-{product['id']}",
            "content": content,
            "metadata": {"product_id": product["id"], "source": "products_db"},
        })
    return {
        "query": query["query"],
        "documents": documents,
        "relevant_ids": [f"product-{item}" for item in sorted(relevant_ids)],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-documents", type=int, default=30)
    args = parser.parse_args()
    if args.max_documents <= 0:
        raise SystemExit("--max-documents 必须大于 0")

    queries = load_queries(args.queries)
    with sqlite3.connect(args.db) as connection:
        cases = [
            build_case(connection, query, max_documents=args.max_documents)
            for query in queries
        ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
