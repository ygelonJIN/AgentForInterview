"""图节点共用的小工具。"""
from __future__ import annotations

import inspect
from typing import Any


async def resolve(value: Any) -> Any:
    """依赖既可能是同步函数，也可能是协程函数。"""
    return await value if inspect.isawaitable(value) else value
