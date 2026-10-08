"""统一执行日志事件，覆盖 LangGraph 分支、重试、循环和降级。"""

from __future__ import annotations

import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from app.reliability import NodePolicy

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


async def run_policy_node(
    policy: NodePolicy,
    operation: Callable[[int], Awaitable[Any]],
    *,
    queue: Any,
    graph: str,
    node: str,
    step: str = "",
    branch: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Any:
    """执行节点并把策略、每次尝试和最终状态发送到可见执行日志。"""
    started = time.perf_counter()
    attempts_used = 0
    policy_details = {
        **(details or {}),
        "timeout_seconds": policy.timeout_seconds,
        "retry_max_attempts": policy.retry_policy.max_attempts,
        "retry_base_delay_seconds": policy.retry_policy.base_delay_seconds,
        "retry_max_delay_seconds": policy.retry_policy.max_delay_seconds,
    }
    await emit_execution_log(
        queue,
        graph=graph,
        node=node,
        kind="node_started",
        status="running",
        step=step,
        branch=branch,
        max_attempts=policy.retry_policy.max_attempts,
        details=policy_details,
        message=(
            f"开始执行；超时 {policy.timeout_seconds:g}s，"
            f"最多尝试 {policy.retry_policy.max_attempts} 次"
        ),
    )

    async def observed(kind: str, payload: Dict[str, Any]) -> None:
        nonlocal attempts_used
        if kind == "attempt_started":
            attempts_used = int(payload.get("attempt", attempts_used + 1))
        status = {
            "attempt_succeeded": "ok",
            "retry_scheduled": "warning",
            "attempt_failed": "warning" if payload.get("will_retry") else "error",
            "retry_exhausted": "error",
        }.get(kind, "info")
        message = {
            "attempt_started": (
                f"第 {payload.get('attempt')}/{payload.get('max_attempts')} 次尝试"
            ),
            "attempt_succeeded": (
                f"第 {payload.get('attempt')} 次尝试成功，"
                f"耗时 {payload.get('duration_ms')}ms"
            ),
            "retry_scheduled": (
                f"第 {payload.get('attempt')} 次失败，"
                f"{payload.get('delay_seconds')}s 后重试第 {payload.get('next_attempt')} 次"
            ),
            "attempt_failed": (
                f"第 {payload.get('attempt')} 次尝试失败："
                f"{payload.get('error_type')}: {payload.get('error')}"
            ),
            "retry_exhausted": (
                (
                    f"尝试上限 {payload.get('max_attempts')} 次已用完："
                    if int(payload.get('max_attempts') or 0) <= 1
                    else f"重试达到上限 {payload.get('max_attempts')} 次："
                )
                + f"{payload.get('error_type')}: {payload.get('error')}"
            ),
        }.get(kind, kind)
        await emit_execution_log(
            queue,
            graph=graph,
            node=node,
            kind=kind,
            status=status,
            step=step,
            branch=branch,
            attempt=payload.get("attempt"),
            max_attempts=payload.get("max_attempts"),
            duration_ms=payload.get("duration_ms"),
            details=payload,
            message=message,
        )

    try:
        result = await policy.run(operation, on_event=observed)
    except Exception as exc:
        await emit_execution_log(
            queue,
            graph=graph,
            node=node,
            kind="node_failed",
            status="error",
            step=step,
            branch=branch,
            attempt=attempts_used or None,
            max_attempts=policy.retry_policy.max_attempts,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            details={"error_type": type(exc).__name__, "error": str(exc)},
            message=f"节点失败：{type(exc).__name__}: {exc}",
        )
        raise

    await emit_execution_log(
        queue,
        graph=graph,
        node=node,
        kind="node_succeeded",
        status="ok",
        step=step,
        branch=branch,
        attempt=attempts_used or 1,
        max_attempts=policy.retry_policy.max_attempts,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        message=f"节点完成，共使用 {attempts_used or 1} 次尝试",
    )
    return result


class CallbackEventQueue:
    """把执行日志适配到 TravelPlanGraph 等 on_event 回调。"""

    def __init__(self, callback: Callable[..., Any]):
        self.callback = callback

    async def emit_execution_log(self, payload: Dict[str, Any]) -> None:
        data = dict(payload.get("data") or {})
        data["step"] = payload.get("step", "")
        result = self.callback(payload.get("event", EXECUTION_LOG_EVENT), data)
        if hasattr(result, "__await__"):
            await result
