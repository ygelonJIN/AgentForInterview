"""FastAPI 依赖：读取配置、校验 Bearer token、取当前账号。"""
from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth_service.config import Settings
from auth_service.db import db_session
from auth_service.repository import find_account_by_id, public_account
from auth_service.security import AuthError, decode_access_token

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def client_meta(request: Request) -> tuple[str, str]:
    ip = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")
    return ip, user_agent


def get_current_account(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """校验签名、过期时间、账号状态和 token 版本，返回公开账号信息。"""
    if credentials is None or not credentials.credentials:
        raise AuthError(401, "missing_token", "请先登录")
    settings: Settings = request.app.state.settings
    claims = decode_access_token(settings, credentials.credentials)
    with db_session(settings.db_path) as conn:
        row = find_account_by_id(conn, claims["sub"])
    if row is None or row["status"] != "active":
        raise AuthError(401, "account_unavailable", "账号不存在或已停用")
    if int(claims.get("ver", 0)) != int(row["token_version"]):
        raise AuthError(401, "token_revoked", "登录状态已失效，请重新登录")
    return public_account(row)
