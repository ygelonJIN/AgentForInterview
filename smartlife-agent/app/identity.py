"""服务端 Actor 上下文与工具身份绑定。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class ActorContext:
    """一次调用的可信用户身份和权限集合。"""

    user_id: str
    session_id: str = ""
    scopes: FrozenSet[str] = frozenset({"read"})

    def __post_init__(self) -> None:
        user_id = str(self.user_id or "").strip()
        if not user_id:
            raise ValueError("actor user_id 不能为空")
        if len(user_id) > 128:
            raise ValueError("actor user_id 过长")
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "scopes", frozenset(self.scopes))

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise PermissionError(f"当前 Actor 缺少权限: {scope}")

    @classmethod
    def local(cls, user_id: str, session_id: str = "") -> "ActorContext":
        return cls(user_id=user_id, session_id=session_id)
