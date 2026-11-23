"""调用身份服务的 HTTP 客户端。

只依赖 httpx，和 Streamlit 解耦，方便命令行脚本或其他前端复用。
"""
from __future__ import annotations

import os

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class AuthAPIError(RuntimeError):
    """身份服务返回的业务错误；status_code=0 表示连不上。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    @property
    def unreachable(self) -> bool:
        return self.status_code == 0


class AuthClient:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0):
        self.base_url = (
            base_url or os.environ.get("AUTH_API_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        token: str | None = None,
    ):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise AuthAPIError(
                0,
                "auth_unreachable",
                "身份服务未启动或无法连接",
            ) from exc

        if response.status_code == 204:
            return None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            error = data.get("error") if isinstance(data, dict) else None
            error = error if isinstance(error, dict) else {}
            raise AuthAPIError(
                response.status_code,
                str(error.get("code") or "request_failed"),
                str(error.get("message") or f"身份服务返回 {response.status_code}"),
            )
        return data

    def register(self, username: str, password: str, display_name: str = "", invite_code: str = "") -> dict:
        return self._request(
            "POST",
            "/v1/auth/register",
            json={
                "username": username,
                "password": password,
                "display_name": display_name or None,
                "invite_code": invite_code or None,
            },
        )

    def login(self, username: str, password: str) -> dict:
        return self._request("POST", "/v1/auth/login", json={"username": username, "password": password})

    def refresh(self, refresh_token: str) -> dict:
        return self._request("POST", "/v1/auth/refresh", json={"refresh_token": refresh_token})

    def logout(self, refresh_token: str) -> None:
        self._request("POST", "/v1/auth/logout", json={"refresh_token": refresh_token})

    def me(self, token: str) -> dict:
        return self._request("GET", "/v1/auth/me", token=token)

    def change_password(self, token: str, old_password: str, new_password: str) -> dict:
        return self._request(
            "POST",
            "/v1/auth/change-password",
            json={"old_password": old_password, "new_password": new_password},
            token=token,
        )

    def profile(self, token: str) -> dict:
        return self._request("GET", "/v1/me/profile", token=token)

    def orders(self, token: str) -> dict:
        return self._request("GET", "/v1/me/orders", token=token)

    def order(self, order_id: int, token: str) -> dict:
        return self._request("GET", f"/v1/me/orders/{int(order_id)}", token=token)
