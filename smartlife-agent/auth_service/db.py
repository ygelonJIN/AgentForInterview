"""SQLite 连接与建表。

服务默认用 SQLite，单机部署足够；所有 SQL 都收在 ``repository.py``，
以后换 Postgres 时只改这一层和连接函数。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    roles TEXT NOT NULL DEFAULT 'user',
    scopes TEXT NOT NULL DEFAULT 'read',
    status TEXT NOT NULL DEFAULT 'active',
    token_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    password_changed_at TEXT,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    refresh_hash TEXT NOT NULL UNIQUE,
    user_agent TEXT,
    ip TEXT,
    created_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at TEXT,
    rotated_from TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    event TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_events_account ON auth_events(account_id, id);
CREATE INDEX IF NOT EXISTS idx_auth_events_created ON auth_events(created_at);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_session(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """一次请求一个连接；正常提交，异常回滚，最后一定关闭。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with db_session(path) as conn:
        conn.executescript(SCHEMA)
    # 账号库里是密码哈希和会话，文件权限收到 0600。
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
