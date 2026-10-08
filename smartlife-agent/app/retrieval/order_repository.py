"""订单只读仓储：用固定查询替代自由 NL2SQL 的跨用户数据访问。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


class OrderAuthorizationError(PermissionError):
    """订单访问缺少用户上下文或范围非法。"""


class OrderRepository:
    def __init__(self, db_path: Optional[str] = None):
        base_dir = Path(__file__).resolve().parents[2]
        self.db_path = Path(db_path) if db_path else base_dir / "data" / "products.db"

    @staticmethod
    def _validate_principal(user_id: str) -> str:
        value = str(user_id or "").strip()
        if not value:
            raise OrderAuthorizationError("订单查询必须提供当前用户身份")
        return value

    def get_order_status(
        self,
        user_id: str,
        order_id: Optional[str] = None,
        *,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        principal = self._validate_principal(user_id)
        if limit <= 0 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        clauses = ["user_id = ?"]
        params: List[Any] = [principal]
        if order_id is not None and str(order_id).strip():
            clauses.append("id = ?")
            params.append(str(order_id).strip())
        params.append(limit)
        sql = f"""
            SELECT id, user_id, product_id, quantity, status, total_price, created_at
            FROM orders
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]
