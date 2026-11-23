"""项目适配层：把身份服务的账号映射到本项目的用户资料与订单。

这里刻意只做"当前登录用户能看自己的数据"这一件事，业务逻辑不进入身份服务，
这样 ``auth_service/`` 可以整体复制到别的项目。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from auth_service.deps import get_current_account

router = APIRouter(prefix="/v1/me", tags=["me"])


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _public_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "age": row["age"],
        "preferences": row["preferences"],
        "budget": row["budget"],
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
    }


def _ensure_profile(conn: sqlite3.Connection, account: dict) -> sqlite3.Row:
    """账号注册后第一次读资料时补一行 users，避免个人中心空白。

    身份服务不知道业务库，所以这个动作放在适配层做；换项目时换成自己的
    "首次登录建档案"即可。
    """
    row = conn.execute("SELECT * FROM users WHERE id = ?", (account["id"],)).fetchone()
    if row is not None:
        return row
    conn.execute(
        "INSERT INTO users (id, name, age, preferences, budget) VALUES (?, ?, NULL, ?, 0)",
        (account["id"], account.get("display_name") or account["username"], ""),
    )
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE id = ?", (account["id"],)).fetchone()


@router.get("/profile")
def profile(request: Request, account: dict = Depends(get_current_account)) -> dict:
    with _connect(Path(request.app.state.products_db)) as conn:
        row = _ensure_profile(conn, account)
        return _public_user(row)


@router.get("/orders")
def orders(request: Request, account: dict = Depends(get_current_account)) -> dict:
    with _connect(Path(request.app.state.products_db)) as conn:
        rows = conn.execute(
            """
            SELECT o.*, p.name AS product_name, p.brand, p.category
            FROM orders o
            JOIN products p ON p.id = o.product_id
            WHERE o.user_id = ?
            ORDER BY o.created_at DESC
            """,
            (account["id"],),
        ).fetchall()
    return {"orders": [dict(row) for row in rows]}


@router.get("/orders/{order_id}")
def order_detail(
    order_id: int,
    request: Request,
    account: dict = Depends(get_current_account),
):
    with _connect(Path(request.app.state.products_db)) as conn:
        row = conn.execute(
            """
            SELECT o.*, p.name AS product_name, p.brand, p.category
            FROM orders o
            JOIN products p ON p.id = o.product_id
            WHERE o.id = ? AND o.user_id = ?
            """,
            (order_id, account["id"]),
        ).fetchone()
    if row is None:
        # 不存在与不属于当前用户都返回 404，避免泄露别人的订单是否存在。
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "订单不存在"}})
    return dict(row)
