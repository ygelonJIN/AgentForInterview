"""测试用注册/登录快捷函数。"""
from __future__ import annotations

from fastapi.testclient import TestClient


def register(client: TestClient, username: str = "alice", password: str = "alice1234") -> dict:
    response = client.post(
        "/v1/auth/register",
        json={"username": username, "password": password, "display_name": username.title()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, username: str = "alice", password: str = "alice1234") -> dict:
    response = client.post("/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()
