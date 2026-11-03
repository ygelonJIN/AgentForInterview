"""统一执行日志事件，覆盖 LangGraph 分支、重试、循环和降级。"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional


EXECUTION_LOG_EVENT = "execution_log"

_SAFE_FIELDS = {"api_key", "authorization", "password", "token"}


def _safe_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if depth >= 3:
        return str(value)[:500]
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]" if str(key).lower() in _SAFE_FIELDS
                else _safe_value(item, depth + 1)
            )
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in list(value)[:20]]
    return str(value)[:500]


def _safe_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _safe_value(dict(details or {}))


def build_execution_log(
    *,
    graph: str,
    node: str,
    kind: str,
    message: str,
    status: str = "info",
    step: str = "",
    attempt: Optional[int] = None,
    max_attempts: Optional[int] = None,
    iteration: Optional[int] = None,
    max_iterations: Optional[int] = None,
    branch: Optional[str] = None,
    duration_ms: Optional[float] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "event": EXECUTION_LOG_EVENT,
        "data": {
            "log_id": uuid.uuid4().hex,
            "graph": graph,
            "node": node,
            "kind": kind,
            "status": status,
            "message": message,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "iteration": iteration,
            "max_iterations": max_iterations,
            "branch": branch,
            "duration_ms": duration_ms,
            "details": _safe_details(details),
        },
        "step": step,
    }


async def emit_execution_log(queue: Any, **kwargs: Any) -> None:
    payload = build_execution_log(**kwargs)
    if hasattr(queue, "emit_execution_log"):
        await queue.emit_execution_log(payload)
    elif hasattr(queue, "emit"):
        await queue.emit(payload["event"], payload["data"], payload.get("step", ""))
