"""从业务数据库构建 Rerank 评测集测试。"""

import importlib.util
import json
import sqlite3
from pathlib import Path


def _load_builder():
    path = Path(__file__).parents[1] / "scripts" / "build_rerank_eval_from_db.py"
    spec = importlib.util.spec_from_file_location("rerank_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builder_uses_business_db_and_query_labels(tmp_path):
    builder = _load_builder()
    db = tmp_path / "products.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE products (id INTEGER, name TEXT, category TEXT, subcategory TEXT, "
            "price REAL, waterproof INTEGER, brand TEXT, stock INTEGER, description TEXT, "
            "rating REAL, created_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE reviews (id INTEGER, product_id INTEGER, user_id TEXT, content TEXT, rating INTEGER, created_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "防水跑鞋", "运动", "跑步鞋", 499, 1, "A", 10, "适合雨天", 4.8, "2026-01-01"),
                (2, "拍照手机", "电子", "手机", 3999, 0, "B", 5, "夜景清晰", 4.7, "2026-01-02"),
            ],
        )
        connection.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1, "u1", "抓地力很好", 5, "2026-01-03"),
        )
    queries = tmp_path / "queries.jsonl"
    queries.write_text(
        json.dumps({"query": "防水跑步鞋", "relevant_product_ids": [1]}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "eval.jsonl"

    # Exercise the pure builder rather than CLI argument parsing.
    with sqlite3.connect(db) as connection:
        case = builder.build_case(
            connection,
            {"query": "防水跑步鞋", "relevant_product_ids": [1]},
            max_documents=10,
        )

    assert case["relevant_ids"] == ["product-1"]
    assert {item["id"] for item in case["documents"]} == {"product-1", "product-2"}
    assert "抓地力很好" in case["documents"][0]["content"]
