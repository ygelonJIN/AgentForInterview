"""账号、会话与审计事件的数据库操作。

所有 SQL 集中在这些函数里；路由层不直接写 SQL。
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Iterable

from auth_service.security import join_list, now_iso, now_ts, split_list, username_key


def public_account(row) -> dict:
    """对外的账号信息，绝不包含密码哈希。"""
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "roles": split_list(row["roles"]),
        "scopes": split_list(row["scopes"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def create_account(
    conn: sqlite3.Connection,
    *,
    username: str,
    password_hash: str,
    display_name: str | None = None,
    roles: Iterable[str] = ("user",),
    scopes: Iterable[str] = ("read",),
    account_id: str | None = None,
    status: str = "active",
) -> dict:
    identifier = account_id or uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO accounts (
            id, username, username_key, password_hash, display_name,
            roles, scopes, status, token_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            identifier,
            username,
            username_key(username),
            password_hash,
            display_name or username,
            join_list(roles),
            join_list(scopes),
            status,
            now_iso(),
        ),
    )
    return dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (identifier,)).fetchone())


def find_account_by_id(conn: sqlite3.Connection, account_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (str(account_id),)).fetchone()
    return dict(row) if row else None


def find_account_by_username(conn: sqlite3.Connection, username: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM accounts WHERE username_key = ?",
        (username_key(username),),
    ).fetchone()
    return dict(row) if row else None


def touch_login(conn: sqlite3.Connection, account_id: str) -> None:
    conn.execute(
        "UPDATE accounts SET last_login_at = ? WHERE id = ?",
        (now_iso(), account_id),
    )


def set_password(conn: sqlite3.Connection, account_id: str, password_hash: str) -> None:
    """改密后 token_version +1，让已经签发的 access token 立即失效。"""
    conn.execute(
        """
        UPDATE accounts
        SET password_hash = ?, password_changed_at = ?, token_version = token_version + 1
        WHERE id = ?
        """,
        (password_hash, now_iso(), account_id),
    )


def create_session(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    refresh_hash: str,
    ttl: int,
    user_agent: str | None = None,
    ip: str | None = None,
    rotated_from: str | None = None,
) -> dict:
    identifier = uuid.uuid4().hex
    expires_at = now_ts() + int(ttl)
    conn.execute(
        """
        INSERT INTO sessions (
            id, account_id, refresh_hash, user_agent, ip,
            created_at, expires_at, rotated_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identifier,
            account_id,
            refresh_hash,
            (user_agent or "")[:300],
            (ip or "")[:64],
            now_iso(),
            expires_at,
            rotated_from,
        ),
    )
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (identifier,)).fetchone()
    return dict(row)


def find_session_by_refresh(conn: sqlite3.Connection, refresh_hash: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE refresh_hash = ?",
        (refresh_hash,),
    ).fetchone()
    return dict(row) if row else None


def revoke_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (now_iso(), session_id),
    )


def revoke_all_sessions(conn: sqlite3.Connection, account_id: str) -> int:
    cursor = conn.execute(
        "UPDATE sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
        (now_iso(), account_id),
    )
    return int(cursor.rowcount or 0)


def cleanup_expired_sessions(conn: sqlite3.Connection, before_ts: int | None = None) -> int:
    cursor = conn.execute(
        "DELETE FROM sessions WHERE expires_at < ? OR (revoked_at IS NOT NULL AND expires_at < ?)",
        (before_ts or now_ts(), before_ts or now_ts()),
    )
    return int(cursor.rowcount or 0)


def log_event(
    conn: sqlite3.Connection,
    event: str,
    *,
    account_id: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO auth_events (account_id, event, ip, user_agent, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            event,
            (ip or "")[:64],
            (user_agent or "")[:300],
            (detail or "")[:500],
            now_iso(),
        ),
    )
