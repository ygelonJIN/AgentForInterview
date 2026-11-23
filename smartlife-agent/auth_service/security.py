"""密码、JWT 与登录限速。

这一层只依赖 bcrypt / PyJWT，不碰数据库和 HTTP，方便单独测试。
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
import uuid
from typing import Iterable, List

import bcrypt
import jwt

from auth_service.config import Settings

# 账号不存在时也跑一次 bcrypt，避免通过响应时间判断用户名是否存在。
# 这是固定字符串的哈希，不对应任何真实密码。
_DUMMY_HASH = "$2b$12$xPbTxc9tdfBIRjL19LprzeFT0FuDFQRxQrv2PCGjv/QXppZw74BYW"

_USERNAME_RE = re.compile(r"^[\w.\-]+$", re.UNICODE)


class AuthError(Exception):
    """带 HTTP 状态码的业务错误，由 FastAPI 统一转成 JSON。"""

    def __init__(self, status_code: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_ts() -> int:
    return int(time.time())


def split_list(value: str | Iterable[str] | None) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def join_list(values: Iterable[str]) -> str:
    return ",".join(split_list(values))


def normalize_username(username: str, settings: Settings) -> str:
    value = str(username or "").strip()
    if not (settings.username_min <= len(value) <= settings.username_max):
        raise AuthError(
            400,
            "invalid_username",
            f"用户名长度需在 {settings.username_min}—{settings.username_max} 个字符之间",
        )
    if not _USERNAME_RE.fullmatch(value):
        raise AuthError(400, "invalid_username", "用户名只能包含字母、数字、汉字、下划线、点和短横线")
    return value


def username_key(username: str) -> str:
    return str(username or "").strip().casefold()


def validate_password(password: str, settings: Settings) -> str:
    if not isinstance(password, str):
        raise AuthError(400, "invalid_password", "密码格式不正确")
    encoded = password.encode("utf-8")
    if len(password) < settings.password_min:
        raise AuthError(400, "weak_password", f"密码至少 {settings.password_min} 位")
    if len(encoded) > settings.password_max_bytes:
        raise AuthError(400, "invalid_password", f"密码不能超过 {settings.password_max_bytes} 字节")
    return password


def hash_password(password: str, rounds: int = 12) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    target = password_hash or _DUMMY_HASH
    try:
        return bool(bcrypt.checkpw(password.encode("utf-8"), target.encode("ascii")))
    except (ValueError, TypeError):
        return False


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(settings: Settings, account) -> tuple[str, int]:
    issued_at = now_ts()
    payload = {
        "sub": str(account["id"]),
        "roles": split_list(account["roles"]),
        "scopes": split_list(account["scopes"]),
        "ver": int(account["token_version"]),
        "iss": settings.issuer,
        "aud": settings.audience,
        "iat": issued_at,
        "exp": issued_at + settings.access_ttl,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, settings.access_ttl


def decode_access_token(settings: Settings, token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.audience,
            issuer=settings.issuer,
            options={"require": ["exp", "sub", "jti", "ver"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError(401, "token_expired", "登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(401, "invalid_token", "无效的登录凭证") from exc


class LoginRateLimiter:
    """进程内失败计数，够单机使用；多实例部署时要换成 Redis。"""

    def __init__(self, attempts: int, window: int):
        self._attempts = max(1, int(attempts))
        self._window = max(1, int(window))
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int | None:
        now = time.time()
        with self._lock:
            values = [stamp for stamp in self._failures.get(key, []) if now - stamp < self._window]
            if not values:
                self._failures.pop(key, None)
                return None
            self._failures[key] = values
            if len(values) < self._attempts:
                return None
            return max(1, int(self._window - (now - min(values))))

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            values = [stamp for stamp in self._failures.get(key, []) if now - stamp < self._window]
            values.append(now)
            self._failures[key] = values
            if len(self._failures) > 10000:
                self._failures.clear()

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
