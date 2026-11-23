"""验证身份服务可以挂到别的 FastAPI 项目下，而不是只能独立进程。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_service.app import create_app
from auth_service.config import Settings


def test_service_can_be_mounted_under_another_app(tmp_path) -> None:
    settings = Settings(
        db_path=tmp_path / "mounted.db",
        jwt_secret="mounted-secret-0123456789abcdef0",
        bcrypt_rounds=4,
    )
    parent = FastAPI()
    parent.mount("/auth", create_app(settings))
    client = TestClient(parent)

    response = client.post(
        "/auth/v1/auth/register",
        json={"username": "mounted", "password": "mounted123"},
    )
    assert response.status_code == 201, response.text
    login = client.post(
        "/auth/v1/auth/login",
        json={"username": "mounted", "password": "mounted123"},
    )
    assert login.status_code == 200
    me = client.get(
        "/auth/v1/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["username"] == "mounted"
