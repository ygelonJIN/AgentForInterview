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
        clauses = ["o.user_id = ?"]
        params: List[Any] = [principal]
        if order_id is not None and str(order_id).strip():
            clauses.append("o.id = ?")
            params.append(str(order_id).strip())
        params.append(limit)
        # 订单行必须带商品名：客服回答"我买了哪些商品"只能靠这一列，
        # 裸 product_id 到 prompt 里就是 None。用 LEFT JOIN，脏外键也别把订单丢掉。
        sql = f"""
            SELECT o.id, o.user_id, o.product_id, o.quantity, o.status,
                   o.total_price, o.created_at,
                   p.name AS product_name, p.category AS product_category
            FROM orders o
            LEFT JOIN products p ON p.id = o.product_id
            WHERE {' AND '.join(clauses)}
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT ?
        """
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def purchased_items(self, user_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """这个用户买过的东西（名称 + 品类 + 数量），用来判断"这次还缺不缺"。

        只算会到手的：取消/退款/退货的不算——否则用户退了货，系统还以为他有。
        与 ``get_order_status`` 共用同一套身份校验：订单只按当前用户查。
        """
        principal = self._validate_principal(user_id)
        if limit <= 0 or limit > 200:
            raise ValueError("limit 必须在 1 到 200 之间")
        sql = """
            SELECT p.name AS name, p.category AS category,
                   p.subcategory AS subcategory, o.quantity AS quantity,
                   o.status AS status, o.created_at AS created_at
            FROM orders o
            JOIN products p ON p.id = o.product_id
            WHERE o.user_id = ?
              AND o.status NOT IN ('cancelled', 'canceled', 'refunded', 'returned')
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT ?
        """
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, (principal, limit)).fetchall()
        return [dict(row) for row in rows]
