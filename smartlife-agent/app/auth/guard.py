"""Streamlit 侧登录门禁、会话保持和身份注入。

原则：身份只从服务端签发的 token 来；页面、消息和模型输出都不能改 ``user_id``。
"""
from __future__ import annotations

import os
import time
import uuid

import streamlit as st

from app.auth.client import AuthAPIError, AuthClient
from app.identity import ActorContext
from app.ui.theme import alert_html, brand_html

REFRESH_COOKIE = "smartlife_refresh"
TRANSIENT_KEYS = (
    "messages",
    "turn_graph",
    "candidate_memories",
    "show_memory_extraction",
    "memory_diag",
    "memory_extraction_error",
    "memory_extraction_scene",
    "memory_saved_map",
    "memory_extraction_thread",
    "last_memory_extract",
    "pending_compressed_summary",
    "active_thread_id",
    "pending_thread_delete",
    "active_page",
    "clarify_draft",
)


def auth_enabled() -> bool:
    raw = os.environ.get("SMARTLIFE_AUTH_DISABLED", "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}


def cookie_enabled() -> bool:
    raw = os.environ.get("SMARTLIFE_AUTH_COOKIE", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _client() -> AuthClient:
    return AuthClient()


def _cookie_manager():
    if not cookie_enabled():
        return None
    # 同一次脚本运行里读写各建一个会撞 key，这里按会话缓存一个实例。
    if "_auth_cookie_manager" in st.session_state:
        return st.session_state["_auth_cookie_manager"]
    try:
        from extra_streamlit_components import CookieManager
    except Exception:
        st.session_state["_auth_cookie_manager"] = None
        return None
    try:
        manager = CookieManager(key="smartlife_auth_cookie")
    except Exception:
        manager = None
    st.session_state["_auth_cookie_manager"] = manager
    return manager


def _read_cookie_refresh() -> str | None:
    # 先读请求自带的 cookie（页面刷新后立刻可用），再退回组件读取。
    try:
        cookies = st.context.cookies
        value = cookies.get(REFRESH_COOKIE) if cookies else None
        if value:
            return str(value)
    except Exception:
        pass
    manager = _cookie_manager()
    if manager is None:
        return None
    try:
        value = manager.get(REFRESH_COOKIE)
    except Exception:
        return None
    return str(value) if value else None


def _write_cookie_refresh(token: str, max_age: int) -> None:
    manager = _cookie_manager()
    if manager is None or not token:
        return
    try:
        manager.set(
            REFRESH_COOKIE,
            token,
            path="/",
            max_age=max(60, int(max_age)),
            same_site="lax",
        )
    except Exception:
        pass


def _delete_cookie_refresh() -> None:
    manager = _cookie_manager()
    if manager is None:
        return
    try:
        manager.delete(REFRESH_COOKIE)
    except Exception:
        pass


def current_account() -> dict | None:
    account = st.session_state.get("auth_account")
    return account if isinstance(account, dict) else None


def _apply_identity(account: dict) -> None:
    session_id = st.session_state.get("browser_session_id") or uuid.uuid4().hex
    st.session_state["browser_session_id"] = session_id
    st.session_state["user_id"] = account["id"]
    st.session_state["actor_context"] = ActorContext(
        user_id=account["id"],
        session_id=session_id,
        scopes=frozenset(account.get("scopes") or ["read"]),
    )


def _clear_auth() -> None:
    for key in ("auth_account", "auth_access", "auth_access_exp", "auth_refresh", "user_id", "actor_context"):
        st.session_state.pop(key, None)
    for key in TRANSIENT_KEYS:
        st.session_state.pop(key, None)


def _save_tokens(payload: dict, *, new_login: bool) -> None:
    account = payload.get("account") or {}
    if not account.get("id"):
        raise RuntimeError("身份服务返回的账号信息不完整")
    if new_login:
        st.session_state["browser_session_id"] = uuid.uuid4().hex
        for key in TRANSIENT_KEYS:
            st.session_state.pop(key, None)
    st.session_state["auth_account"] = account
    st.session_state["auth_access"] = payload.get("access_token") or ""
    st.session_state["auth_access_exp"] = time.time() + float(payload.get("expires_in") or 0)
    st.session_state["auth_refresh"] = payload.get("refresh_token") or ""
    _apply_identity(account)
    _write_cookie_refresh(
        st.session_state["auth_refresh"],
        int(payload.get("refresh_expires_in") or 7 * 24 * 60 * 60),
    )


def refresh_access_token() -> bool:
    refresh = st.session_state.get("auth_refresh") or _read_cookie_refresh()
    if not refresh:
        return False
    try:
        payload = _client().refresh(refresh)
    except AuthAPIError:
        _clear_auth()
        return False
    _save_tokens(payload, new_login=False)
    return True


def call_api(method: str, **kwargs):
    """带自动续期的身份服务调用；401 时刷新一次再重试。"""
    token = st.session_state.get("auth_access") or ""
    function = getattr(_client(), method)
    try:
        return function(token=token, **kwargs)
    except AuthAPIError as exc:
        if exc.status_code == 401 and refresh_access_token():
            return getattr(_client(), method)(token=st.session_state.get("auth_access"), **kwargs)
        raise


def _friendly_error(exc: AuthAPIError) -> str:
    if exc.unreachable:
        return "身份服务未启动。请在项目目录运行 ./run.sh，它会同时启动 8000 和 8501。"
    return exc.message


def _render_login(*, error: str | None = None, notice: str | None = None) -> None:
    st.markdown(
        "<style>[data-testid='stSidebar'],[data-testid='collapsedControl']{display:none;}</style>",
        unsafe_allow_html=True,
    )
    _, center, _ = st.columns([1, 1.15, 1])
    with center:
        st.markdown(brand_html(), unsafe_allow_html=True)
        st.markdown(
            "<h1 style='margin:1.2rem 0 .2rem;font-size:2rem;'>登录 SmartLife</h1>",
            unsafe_allow_html=True,
        )
        st.caption("账号、密码哈希和全部数据都在这台机器上；身份由本地身份服务签发。")
        if error:
            st.markdown(alert_html("danger", "登录失败", error), unsafe_allow_html=True)
        elif notice:
            st.markdown(alert_html("warn", "需要登录", notice), unsafe_allow_html=True)

        login_tab, register_tab = st.tabs(["登录", "注册"])
        with login_tab:
            with st.form("login_form"):
                username = st.text_input("用户名", key="login_username", placeholder="例如 demo")
                password = st.text_input("密码", type="password", key="login_password")
                submitted = st.form_submit_button("登录", type="primary", use_container_width=True)
            if submitted:
                _submit_login(username, password)
        with register_tab:
            with st.form("register_form"):
                new_username = st.text_input("用户名", key="register_username", placeholder="3—32 个字符")
                display_name = st.text_input("显示名（可选）", key="register_display")
                new_password = st.text_input("密码（至少 8 位）", type="password", key="register_password")
                confirm = st.text_input("确认密码", type="password", key="register_confirm")
                invite_code = st.text_input("邀请码（如已开启）", key="register_invite")
                registered = st.form_submit_button("注册并登录", type="primary", use_container_width=True)
            if registered:
                _submit_register(new_username, display_name, new_password, confirm, invite_code)


def _submit_login(username: str, password: str) -> None:
    username = str(username or "").strip()
    if not username or not password:
        st.error("请输入用户名和密码。")
        return
    try:
        payload = _client().login(username, password)
    except AuthAPIError as exc:
        st.error(_friendly_error(exc))
        return
    _save_tokens(payload, new_login=True)
    st.rerun()


def _submit_register(
    username: str,
    display_name: str,
    password: str,
    confirm: str,
    invite_code: str,
) -> None:
    username = str(username or "").strip()
    if not username:
        st.error("请输入用户名。")
        return
    if len(str(password or "")) < 8:
        st.error("密码至少 8 位。")
        return
    if password != confirm:
        st.error("两次输入的密码不一致。")
        return
    try:
        _client().register(username, password, display_name, invite_code)
        payload = _client().login(username, password)
    except AuthAPIError as exc:
        st.error(_friendly_error(exc))
        return
    _save_tokens(payload, new_login=True)
    st.rerun()


def ensure_authenticated() -> dict | None:
    """未登录就渲染登录页并停住；认证关闭时返回 None，保持原有单 Actor 行为。"""
    if not auth_enabled():
        return None

    account = current_account()
    access = st.session_state.get("auth_access") or ""
    expires_at = float(st.session_state.get("auth_access_exp") or 0)
    if account and access and expires_at > time.time() + 30:
        _apply_identity(account)
        return account

    refresh = st.session_state.get("auth_refresh") or _read_cookie_refresh()
    if refresh:
        try:
            payload = _client().refresh(refresh)
        except AuthAPIError as exc:
            _clear_auth()
            if exc.unreachable:
                _render_login(error=_friendly_error(exc))
            else:
                _delete_cookie_refresh()
                _render_login(notice="登录已过期，请重新登录")
            st.stop()
        _save_tokens(payload, new_login=False)
        st.rerun()

    _render_login()
    st.stop()


def is_admin() -> bool:
    if not auth_enabled():
        return True
    account = current_account()
    return bool(account and "admin" in (account.get("roles") or []))


def can_access_page(page_key: str) -> bool:
    if page_key in {"import", "agents"}:
        return is_admin()
    return True


def change_password(old_password: str, new_password: str) -> None:
    """改密成功后服务端会让旧 token 失效，这里同步换成新的一对。"""
    payload = call_api(
        "change_password",
        old_password=old_password,
        new_password=new_password,
    )
    _save_tokens(payload, new_login=False)


def logout() -> None:
    refresh = st.session_state.get("auth_refresh")
    if refresh:
        try:
            _client().logout(refresh)
        except AuthAPIError:
            pass
    _delete_cookie_refresh()
    _clear_auth()
    st.rerun()
