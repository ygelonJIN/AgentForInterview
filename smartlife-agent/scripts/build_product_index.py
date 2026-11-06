#!/usr/bin/env python3
"""建立/重建商品索引：2-gram 检索索引 + 品类摘要物化表。

导入商品后跑一次，读路径就不再全表扫描、也不再每次 GROUP BY。

用法（在项目根目录）::

    python scripts/build_product_index.py            # 建到 data/products.db
    python scripts/build_product_index.py --stats    # 只看索引状态，不建

商品超过 5 万行时，检索路径不会自动建索引（避免请求里跑全量构建），
所以导完数据要手动跑一次这个脚本。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.retrieval import search_index  # noqa: E402
from app.trip_gear.catalog import refresh_summary  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "products.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="商品检索索引（2-gram）构建")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--stats", action="store_true", help="只输出状态")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"数据库不存在：{path}")
        return 1

    with sqlite3.connect(path) as connection:
        products = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        indexed = -1
        try:
            indexed = connection.execute(f"SELECT COUNT(*) FROM {search_index._FTS_TABLE}").fetchone()[0]
        except sqlite3.DatabaseError:
            pass

        if args.stats:
            print(f"商品 {products} 行，检索索引 {indexed} 行")
            return 0

        started = time.perf_counter()
        written = search_index.build(connection)
        categories = refresh_summary(connection)
        elapsed = time.perf_counter() - started

    search_index.reset_cache(str(path))
    print(f"已建立检索索引：{written} 条，品类摘要：{categories} 个，用时 {elapsed:.2f}s")
    print(f"数据库：{path}（{path.stat().st_size / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
