"""商品和评价的固定、参数化只读查询。"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.retrieval import search_index


class ProductRepository:
    def __init__(self, db_path: Optional[str] = None):
        base_dir = Path(__file__).resolve().parents[2]
        self.db_path = Path(db_path) if db_path else base_dir / "data" / "products.db"

    @staticmethod
    def _terms(query: str) -> List[str]:
        raw = re.split(r"[\s,，。；;、/|]+", str(query or "").strip())
        return [item for item in raw if item][:8]

    def search(
        self,
        query: str = "",
        *,
        category: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        brand: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按关键词与结构化条件查商品。

        有关键词时优先走 2-gram 索引（`search_index`），索引不可用才退回
        ``LIKE '%词%'``——后者是线性扫描，商品上十万行之后会明显变慢。
        """
        if limit <= 0 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("min_price 不能大于 max_price")

        terms = self._terms(query)
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row

            use_index = bool(terms) and search_index.ensure(connection, str(self.db_path))
            expression = search_index.match_expression(terms) if use_index else None

            clauses: List[str] = []
            params: List[Any] = []

            if expression:
                # 索引负责"哪些行可能相关"，LIKE 负责"逐字核实"，
                # 排序与 LIMIT 仍在 SQL 里做——不能先截断再排序，否则结果会和
                # 全表 LIKE 不一致（20 万行时实测过）。
                join = "JOIN products_fts ON products_fts.rowid = products.id"
                clauses.append("products_fts MATCH ?")
                params.append(expression)
                # 两字词的 2-gram 本身就是精确子串，不用再核；
                # 三字以上是"两个 2-gram 都出现"，可能不相邻，需要 LIKE 复核。
                for term in terms:
                    if len(term) < 3:
                        continue
                    clauses.append(
                        "(products.name LIKE ? OR products.description LIKE ? "
                        "OR products.subcategory LIKE ? OR products.brand LIKE ?)"
                    )
                    pattern = f"%{term}%"
                    params.extend([pattern, pattern, pattern, pattern])
            else:
                join = ""
                for term in terms:
                    clauses.append(
                        "(products.name LIKE ? OR products.description LIKE ? "
                        "OR products.subcategory LIKE ? OR products.brand LIKE ?)"
                    )
                    pattern = f"%{term}%"
                    params.extend([pattern, pattern, pattern, pattern])

            if category:
                clauses.append("(products.category = ? OR products.subcategory = ?)")
                params.extend([category, category])
            if min_price is not None:
                clauses.append("products.price >= ?")
                params.append(float(min_price))
            if max_price is not None:
                clauses.append("products.price <= ?")
                params.append(float(max_price))
            if brand:
                clauses.append("products.brand = ?")
                params.append(brand)

            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            sql = f"""
                SELECT products.id, products.name, products.category, products.subcategory,
                       products.price, products.waterproof, products.brand, products.stock,
                       products.description, products.rating, products.created_at,
                       products.specs, products.lead_time_days
                FROM products
                {join}
                {where}
                ORDER BY products.rating DESC, products.price ASC, products.id ASC
                LIMIT ?
            """
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]


class ReviewRepository:
    def __init__(self, db_path: Optional[str] = None):
        base_dir = Path(__file__).resolve().parents[2]
        self.db_path = Path(db_path) if db_path else base_dir / "data" / "products.db"

    def get_reviews(
        self,
        product_id: str,
        *,
        aspect: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        try:
            numeric_id = int(str(product_id).strip())
        except ValueError as exc:
            raise ValueError("product_id 必须是数字 ID") from exc
        if limit <= 0 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        clauses = ["product_id = ?"]
        params: List[Any] = [numeric_id]
        if aspect and str(aspect).strip():
            clauses.append("content LIKE ?")
            params.append(f"%{str(aspect).strip()}%")
        params.append(limit)
        sql = f"""
            SELECT id, product_id, user_id, content, rating, created_at
            FROM reviews
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]
