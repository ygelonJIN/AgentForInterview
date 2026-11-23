"""身份服务配置。

所有配置都从环境变量读取，不依赖任何具体项目；这样同一个服务可以被别的
项目直接复制或安装使用，只需要换环境变量。
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple

DEFAULT_DB_PATH = "data/accounts.db"
DEFAULT_SECRET_FILE = "data/auth_secret.key"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前为 {raw!r}") from exc


def _env_list(name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _load_or_create_secret(path: Path) -> str:
    """优先读已保存的密钥；没有就生成一个并落盘。

    密钥每次启动都重新生成会让所有已签发 token 失效，所以必须持久化。
    文件权限收成 0600，避免同机其他用户直接读走。
    """
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value


@dataclass(frozen=True)
class Settings:
    """一次部署的身份服务配置。"""

    db_path: Path = field(default_factory=lambda: Path(DEFAULT_DB_PATH))
    jwt_secret: str = ""
    issuer: str = "smartlife-auth"
    audience: str = "smartlife"
    access_ttl: int = 2 * 60 * 60
    refresh_ttl: int = 7 * 24 * 60 * 60
    bcrypt_rounds: int = 12
    allow_register: bool = True
    invite_code: str = ""
    cors_origins: Tuple[str, ...] = (
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    )
    rate_limit_attempts: int = 5
    rate_limit_window: int = 5 * 60
    username_min: int = 3
    username_max: int = 32
    password_min: int = 8
    password_max_bytes: int = 72

    def __post_init__(self) -> None:
        if not str(self.jwt_secret).strip():
            raise RuntimeError("jwt_secret 不能为空")
        if self.access_ttl <= 0 or self.refresh_ttl <= 0:
            raise RuntimeError("token 有效期必须大于 0")
        if self.refresh_ttl <= self.access_ttl:
            raise RuntimeError("refresh 有效期必须大于 access 有效期")

    @classmethod
    def from_env(cls, *, base_dir: Path | None = None) -> "Settings":
        root = Path(base_dir or os.getcwd()).resolve()
        db_raw = os.environ.get("AUTH_DB_PATH", DEFAULT_DB_PATH)
        db_path = Path(db_raw)
        if not db_path.is_absolute():
            db_path = root / db_path
        secret = os.environ.get("AUTH_JWT_SECRET", "").strip()
        if not secret:
            secret_file = Path(os.environ.get("AUTH_SECRET_FILE", DEFAULT_SECRET_FILE))
            if not secret_file.is_absolute():
                secret_file = root / secret_file
            secret = _load_or_create_secret(secret_file)
        return cls(
            db_path=db_path,
            jwt_secret=secret,
            issuer=os.environ.get("AUTH_ISSUER", cls.issuer),
            audience=os.environ.get("AUTH_AUDIENCE", cls.audience),
            access_ttl=_env_int("AUTH_ACCESS_TTL", cls.access_ttl),
            refresh_ttl=_env_int("AUTH_REFRESH_TTL", cls.refresh_ttl),
            bcrypt_rounds=_env_int("AUTH_BCRYPT_ROUNDS", cls.bcrypt_rounds),
            allow_register=_env_bool("AUTH_ALLOW_REGISTER", cls.allow_register),
            invite_code=os.environ.get("AUTH_INVITE_CODE", "").strip(),
            cors_origins=_env_list("AUTH_CORS_ORIGINS", cls.cors_origins),
            rate_limit_attempts=_env_int("AUTH_RATE_LIMIT_ATTEMPTS", cls.rate_limit_attempts),
            rate_limit_window=_env_int("AUTH_RATE_LIMIT_WINDOW", cls.rate_limit_window),
        )
