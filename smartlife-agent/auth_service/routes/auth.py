"""账号与会话接口。"""
from __future__ import annotations

import secrets
import sqlite3

from fastapi import APIRouter, Depends, Request, Response

from auth_service.config import Settings
from auth_service.db import db_session
from auth_service.deps import client_meta, get_current_account, get_settings
from auth_service.repository import (
    create_account,
    create_session,
    find_account_by_id,
    find_account_by_username,
    find_session_by_refresh,
    log_event,
    public_account,
    revoke_all_sessions,
    revoke_session,
    set_password,
    touch_login,
)
from auth_service.schemas import (
    AccountOut,
    ChangePasswordIn,
    LoginIn,
    LogoutIn,
    RefreshIn,
    RegisterIn,
    TokenPairOut,
)
from auth_service.security import (
    AuthError,
    create_access_token,
    hash_password,
    hash_refresh,
    new_refresh_token,
    normalize_username,
    now_ts,
    username_key,
    validate_password,
    verify_password,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _issue_tokens(
    conn,
    settings: Settings,
    account: dict,
    request: Request,
    *,
    rotated_from: str | None = None,
) -> TokenPairOut:
    refresh_token = new_refresh_token()
    ip, user_agent = client_meta(request)
    create_session(
        conn,
        account_id=account["id"],
        refresh_hash=hash_refresh(refresh_token),
        ttl=settings.refresh_ttl,
        user_agent=user_agent,
        ip=ip,
        rotated_from=rotated_from,
    )
    access_token, expires_in = create_access_token(settings, account)
    return TokenPairOut(
        access_token=access_token,
        expires_in=expires_in,
        refresh_expires_in=settings.refresh_ttl,
        refresh_token=refresh_token,
        account=AccountOut(**public_account(account)),
    )


def _limiter_keys(ip: str, username: str) -> list[str]:
    return [f"ip:{ip}", f"user:{username_key(username)}"]


def _check_rate_limit(request: Request, ip: str, username: str) -> list[str]:
    keys = _limiter_keys(ip, username)
    limiter = request.app.state.login_limiter
    retry = max((limiter.retry_after(key) or 0) for key in keys)
    if retry > 0:
        raise AuthError(
            429,
            "too_many_attempts",
            f"尝试次数过多，请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )
    return keys


@router.post("/register", status_code=201, response_model=AccountOut)
def register(payload: RegisterIn, request: Request, settings: Settings = Depends(get_settings)) -> AccountOut:
    if not settings.allow_register:
        raise AuthError(403, "registration_closed", "当前未开放注册")
    if settings.invite_code and not secrets.compare_digest(
        settings.invite_code, str(payload.invite_code or "")
    ):
        raise AuthError(403, "invalid_invite_code", "邀请码不正确")

    username = normalize_username(payload.username, settings)
    password = validate_password(payload.password, settings)
    display_name = str(payload.display_name or "").strip()[:64] or username
    password_hash = hash_password(password, settings.bcrypt_rounds)
    ip, user_agent = client_meta(request)

    with db_session(settings.db_path) as conn:
        if find_account_by_username(conn, username):
            raise AuthError(409, "username_taken", "该用户名已被注册")
        try:
            account = create_account(
                conn,
                username=username,
                password_hash=password_hash,
                display_name=display_name,
            )
        except sqlite3.IntegrityError as exc:
            raise AuthError(409, "username_taken", "该用户名已被注册") from exc
        log_event(conn, "register", account_id=account["id"], ip=ip, user_agent=user_agent)
    return AccountOut(**public_account(account))


@router.post("/login", response_model=TokenPairOut)
def login(payload: LoginIn, request: Request, settings: Settings = Depends(get_settings)) -> TokenPairOut:
    ip, user_agent = client_meta(request)
    keys = _check_rate_limit(request, ip, payload.username)

    with db_session(settings.db_path) as conn:
        account = find_account_by_username(conn, payload.username)

    # 账号不存在也跑一次 bcrypt，响应时间与"密码错"一致。
    ok = verify_password(payload.password, account["password_hash"] if account else None)
    if account is None or not ok:
        limiter = request.app.state.login_limiter
        for key in keys:
            limiter.record_failure(key)
        with db_session(settings.db_path) as conn:
            log_event(
                conn,
                "login_failed",
                account_id=account["id"] if account else None,
                ip=ip,
                user_agent=user_agent,
            )
        raise AuthError(401, "invalid_credentials", "用户名或密码错误")
    if account["status"] != "active":
        raise AuthError(403, "account_disabled", "账号已被停用")

    for key in keys:
        request.app.state.login_limiter.reset(key)
    with db_session(settings.db_path) as conn:
        touch_login(conn, account["id"])
        account = find_account_by_id(conn, account["id"])
        payload_out = _issue_tokens(conn, settings, account, request)
        log_event(conn, "login", account_id=account["id"], ip=ip, user_agent=user_agent)
    return payload_out


@router.post("/refresh", response_model=TokenPairOut)
def refresh(payload: RefreshIn, request: Request, settings: Settings = Depends(get_settings)) -> TokenPairOut:
    ip, user_agent = client_meta(request)
    token_hash = hash_refresh(payload.refresh_token)
    with db_session(settings.db_path) as conn:
        session = find_session_by_refresh(conn, token_hash)
        if session is None:
            raise AuthError(401, "invalid_refresh", "登录状态无效，请重新登录")
        if session["revoked_at"]:
            log_event(
                conn,
                "refresh_reuse",
                account_id=session["account_id"],
                ip=ip,
                user_agent=user_agent,
            )
            raise AuthError(401, "invalid_refresh", "登录状态已失效，请重新登录")
        if int(session["expires_at"]) <= now_ts():
            raise AuthError(401, "refresh_expired", "登录状态已过期，请重新登录")
        account = find_account_by_id(conn, session["account_id"])
        if account is None or account["status"] != "active":
            raise AuthError(401, "account_unavailable", "账号不存在或已停用")
        revoke_session(conn, session["id"])
        payload_out = _issue_tokens(conn, settings, account, request, rotated_from=session["id"])
        log_event(conn, "refresh", account_id=account["id"], ip=ip, user_agent=user_agent)
    return payload_out


@router.post("/logout", status_code=204)
def logout(
    payload: LogoutIn,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    ip, user_agent = client_meta(request)
    if payload.refresh_token:
        with db_session(settings.db_path) as conn:
            session = find_session_by_refresh(conn, hash_refresh(payload.refresh_token))
            if session is not None:
                revoke_session(conn, session["id"])
                log_event(
                    conn,
                    "logout",
                    account_id=session["account_id"],
                    ip=ip,
                    user_agent=user_agent,
                )
    return Response(status_code=204)


@router.get("/me", response_model=AccountOut)
def me(account: dict = Depends(get_current_account)) -> AccountOut:
    return AccountOut(**account)


@router.post("/change-password", response_model=TokenPairOut)
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    account: dict = Depends(get_current_account),
    settings: Settings = Depends(get_settings),
) -> TokenPairOut:
    with db_session(settings.db_path) as conn:
        row = find_account_by_id(conn, account["id"])
    if row is None or not verify_password(payload.old_password, row["password_hash"]):
        raise AuthError(400, "invalid_password", "原密码不正确")
    new_password = validate_password(payload.new_password, settings)
    new_hash = hash_password(new_password, settings.bcrypt_rounds)
    ip, user_agent = client_meta(request)
    with db_session(settings.db_path) as conn:
        set_password(conn, account["id"], new_hash)
        revoke_all_sessions(conn, account["id"])
        fresh = find_account_by_id(conn, account["id"])
        payload_out = _issue_tokens(conn, settings, fresh, request)
        log_event(conn, "change_password", account_id=account["id"], ip=ip, user_agent=user_agent)
    return payload_out
