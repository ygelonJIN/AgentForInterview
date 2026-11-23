"""账号、会话与 token 生命周期的接口测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from auth_service.app import create_app
from auth_service.config import Settings
from auth_service.tests.helpers import login, register


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_register_login_and_me(client: TestClient) -> None:
    account = register(client)
    assert account["username"] == "alice"
    assert account["roles"] == ["user"]
    assert "password" not in account and "password_hash" not in account

    tokens = login(client)
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0
    me = client.get("/v1/auth/me", headers=auth_header(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["id"] == account["id"]


def test_duplicate_username_is_case_insensitive(client: TestClient) -> None:
    register(client, "Alice")
    response = client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "another123"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "username_taken"


def test_wrong_password_does_not_leak_account_existence(client: TestClient) -> None:
    register(client)
    wrong = client.post("/v1/auth/login", json={"username": "alice", "password": "wrongpass"})
    missing = client.post("/v1/auth/login", json={"username": "nobody", "password": "wrongpass"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["error"]["message"] == missing.json()["error"]["message"]


def test_me_requires_valid_token(client: TestClient) -> None:
    assert client.get("/v1/auth/me").status_code == 401
    assert client.get("/v1/auth/me", headers=auth_header("not-a-token")).status_code == 401


def test_refresh_rotates_and_rejects_reuse(client: TestClient) -> None:
    register(client)
    tokens = login(client)
    first = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200
    second_tokens = first.json()
    assert second_tokens["refresh_token"] != tokens["refresh_token"]

    reuse = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "invalid_refresh"

    # 新的 refresh 仍然可用
    third = client.post("/v1/auth/refresh", json={"refresh_token": second_tokens["refresh_token"]})
    assert third.status_code == 200


def test_logout_revokes_refresh(client: TestClient) -> None:
    register(client)
    tokens = login(client)
    assert client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_change_password_invalidates_old_tokens(client: TestClient) -> None:
    register(client)
    tokens = login(client)
    changed = client.post(
        "/v1/auth/change-password",
        headers=auth_header(tokens["access_token"]),
        json={"old_password": "alice1234", "new_password": "newpass1234"},
    )
    assert changed.status_code == 200
    fresh = changed.json()

    assert client.get("/v1/auth/me", headers=auth_header(tokens["access_token"])).status_code == 401
    assert client.get("/v1/auth/me", headers=auth_header(fresh["access_token"])).status_code == 200
    assert client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert login(client, password="newpass1234")["access_token"]


def test_password_policy(client: TestClient) -> None:
    short = client.post("/v1/auth/register", json={"username": "bob", "password": "short"})
    assert short.status_code == 400
    assert short.json()["error"]["code"] in {"weak_password", "invalid_password"}


def test_registration_can_be_closed(tmp_path) -> None:
    closed = create_app(
        Settings(
            db_path=tmp_path / "closed.db",
            jwt_secret="closed-secret",
            bcrypt_rounds=4,
            allow_register=False,
        )
    )
    client = TestClient(closed)
    response = client.post("/v1/auth/register", json={"username": "bob", "password": "bob123456"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "registration_closed"


def test_invite_code(tmp_path) -> None:
    invited = create_app(
        Settings(
            db_path=tmp_path / "invited.db",
            jwt_secret="invite-secret",
            bcrypt_rounds=4,
            invite_code="camp-2026",
        )
    )
    client = TestClient(invited)
    missing = client.post("/v1/auth/register", json={"username": "bob", "password": "bob123456"})
    assert missing.status_code == 403
    ok = client.post(
        "/v1/auth/register",
        json={"username": "bob", "password": "bob123456", "invite_code": "camp-2026"},
    )
    assert ok.status_code == 201


def test_login_rate_limit(tmp_path) -> None:
    app = create_app(
        Settings(
            db_path=tmp_path / "limited.db",
            jwt_secret="limit-secret",
            bcrypt_rounds=4,
            rate_limit_attempts=3,
            rate_limit_window=60,
        )
    )
    client = TestClient(app)
    register(client)
    for _ in range(3):
        assert client.post("/v1/auth/login", json={"username": "alice", "password": "wrongpass"}).status_code == 401
    blocked = client.post("/v1/auth/login", json={"username": "alice", "password": "alice1234"})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_attempts"
