"""商品和评价的固定、参数化只读查询。"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        if limit <= 0 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("min_price 不能大于 max_price")
        clauses: List[str] = []
        params: List[Any] = []
        for term in self._terms(query):
            clauses.append("(name LIKE ? OR description LIKE ? OR subcategory LIKE ? OR brand LIKE ?)")
            pattern = f"%{term}%"
            params.extend([pattern, pattern, pattern, pattern])
        if category:
            clauses.append("(category = ? OR subcategory = ?)")
            params.extend([category, category])
        if min_price is not None:
            clauses.append("price >= ?")
            params.append(float(min_price))
        if max_price is not None:
            clauses.append("price <= ?")
            params.append(float(max_price))
        if brand:
            clauses.append("brand = ?")
            params.append(brand)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"""
            SELECT id, name, category, subcategory, price, waterproof, brand,
                   stock, description, rating, created_at
            FROM products
            {where}
            ORDER BY rating DESC, price ASC, id ASC
            LIMIT ?
        """
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
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
