"""项目适配接口：只返回当前登录用户的数据。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_server
from auth_service.config import Settings
from auth_service.tests.helpers import login, register


def _build_products_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                age INTEGER,
                preferences TEXT,
                budget REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                brand TEXT,
                category TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',
                total_price REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO products (name, brand, category) VALUES ('帐篷', '牧高笛', '户外'), ('睡袋', '挪客', '户外');
            """
        )


@pytest.fixture
def api(tmp_path):
    products_db = tmp_path / "products.db"
    _build_products_db(products_db)
    settings = Settings(
        db_path=tmp_path / "accounts.db",
        jwt_secret="adapter-secret-0123456789abcdef0",
        bcrypt_rounds=4,
    )
    return TestClient(create_server(settings, products_db)), products_db


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_me_endpoints_only_return_current_user_data(api) -> None:
    client, products_db = api
    alice = register(client, "alice", "alice1234")
    bob = register(client, "bob", "bob123456")
    alice_token = login(client, "alice", "alice1234")["access_token"]
    bob_token = login(client, "bob", "bob123456")["access_token"]

    with sqlite3.connect(products_db) as conn:
        conn.execute(
            "INSERT INTO orders (user_id, product_id, quantity, total_price) VALUES (?, 1, 1, 199)",
            (alice["id"],),
        )
        alice_order = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO orders (user_id, product_id, quantity, total_price) VALUES (?, 2, 1, 299)",
            (bob["id"],),
        )
        bob_order = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    alice_orders = client.get("/v1/me/orders", headers=_bearer(alice_token))
    assert alice_orders.status_code == 200
    names = [item["product_name"] for item in alice_orders.json()["orders"]]
    assert names == ["帐篷"]

    # 即使把别人的 user_id 塞进查询参数，也不会改变结果
    injected = client.get(
        f"/v1/me/orders?user_id={bob['id']}",
        headers=_bearer(alice_token),
    )
    assert [item["product_name"] for item in injected.json()["orders"]] == ["帐篷"]

    assert client.get(f"/v1/me/orders/{alice_order}", headers=_bearer(alice_token)).status_code == 200
    stolen = client.get(f"/v1/me/orders/{bob_order}", headers=_bearer(alice_token))
    assert stolen.status_code == 404

    profile = client.get("/v1/me/profile", headers=_bearer(alice_token))
    assert profile.status_code == 200
    assert profile.json()["id"] == alice["id"]
    assert profile.json()["name"] == "Alice"


def test_me_endpoints_require_token(api) -> None:
    client, _ = api
    assert client.get("/v1/me/profile").status_code == 401
    assert client.get("/v1/me/orders").status_code == 401
    assert client.get("/v1/me/orders", headers=_bearer("broken")).status_code == 401
