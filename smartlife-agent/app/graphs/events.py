"""LangGraph 原生事件出口。

设计约束：**不向 ``config["configurable"]`` 注入事件队列**。
节点通过 ``Runtime.stream_writer`` 把事件写成 custom chunk，顶层用
``astream(stream_mode="custom", subgraphs=True)`` 消费，子图事件天然透传。

``EventSink`` 的方法与语义化事件一一对应，业务代码只依赖它，不关心底层实现。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from app.observability import turn_usage
from app.streaming import EventType, _event_type_value


@dataclass
class TurnTiming:
    """一轮对话的耗时口径：首 token、回答就绪、链路总耗时。"""

    started_monotonic: float = field(default_factory=time.monotonic)
    first_token_monotonic: Optional[float] = None
    response_ready_monotonic: Optional[float] = None

    def mark_token(self) -> None:
        if self.first_token_monotonic is None:
            self.first_token_monotonic = time.monotonic()

    def mark_response_ready(self) -> None:
        self.response_ready_monotonic = time.monotonic()

    def metrics(self) -> Dict[str, Any]:
        now = time.monotonic()
        first_token = self.first_token_monotonic
        response_ready = self.response_ready_monotonic
        return {
            "pipeline_ms": round((now - self.started_monotonic) * 1000, 3),
            "time_to_first_token_ms": (
                round((first_token - self.started_monotonic) * 1000, 3)
                if first_token is not None
                else None
            ),
            "response_ready_ms": (
                round((response_ready - self.started_monotonic) * 1000, 3)
                if response_ready is not None
                else None
            ),
            "post_response_ms": (
                round((now - response_ready) * 1000, 3)
                if response_ready is not None
                else None
            ),
        }


class EventSink:
    """节点内事件出口；每个节点用 ``context.sink_for(runtime)`` 现取。

    writer 是 LangGraph 的 ``Runtime.stream_writer``，同步可调用；
    ``emit`` 保持 async 只为与旧调用点兼容。
    """

    def __init__(
        self,
        writer: Callable[[Any], None],
        timing: Optional[TurnTiming] = None,
        trace_id: str = "",
    ):
        if writer is None:
            raise RuntimeError("缺少 LangGraph stream writer，节点必须在图内执行")
        self._writer = writer
        self.timing = timing or TurnTiming()
        self.trace_id = trace_id

    # ---- 底层 ----
    def _write(self, event_type: Any, data: Dict[str, Any], step: str = "") -> None:
        self._writer({
            "event": _event_type_value(event_type),
            "data": data,
            "step": step,
            "timestamp": time.time(),
            "trace_id": self.trace_id,
        })

    async def emit(self, event_type: Any, data: Dict[str, Any], step: str = "") -> None:
        self._write(event_type, data, step)

    # ---- 语义化事件 ----
    async def emit_thinking(self, message: str, step: str = "", model: str = "") -> None:
        data = {"message": message}
        if model:
            data["model"] = model
        self._write(EventType.THINKING, data, step)

    async def emit_token(self, token: str, step: str = "", section: str = "") -> None:
        self.timing.mark_token()
        data = {"token": token}
        if section:
            data["section"] = section
        self._write(EventType.TOKEN, data, step)

    async def emit_response_reset(self, step: str = "", section: str = "") -> None:
        data = {"section": section} if section else {}
        self._write(EventType.RESPONSE_RESET, data, step)

    async def emit_tool_call(self, tool_name: str, tool_input: Any = None, step: str = "") -> None:
        data = {"tool": tool_name}
        if tool_input is not None:
            data["input"] = str(tool_input)[:500]
        self._write(EventType.TOOL_CALL, data, step)

    async def emit_tool_result(self, tool_name: str, output: str, step: str = "") -> None:
        self._write(EventType.TOOL_RESULT, {"tool": tool_name, "output": str(output)[:1000]}, step)

    async def emit_execution_log(self, payload: Dict[str, Any], step: str = "") -> None:
        self._write(
            payload.get("event", EventType.EXECUTION_LOG),
            dict(payload.get("data") or {}),
            payload.get("step", step),
        )

    async def emit_error(self, message: str, step: str = "") -> None:
        self._write(EventType.ERROR, {"message": message}, step)

    async def emit_done(self, extra: Optional[Dict[str, Any]] = None) -> None:
        self.timing.mark_response_ready()
        timing = self.timing.metrics()
        data = {
            "total_time": f"{(timing['response_ready_ms'] or 0) / 1000:.1f}s",
            "timing_ms": timing,
        }
        # 本轮 Token 用量：从各模型 span 按 trace_id 汇总，没有就不带这个字段。
        usage = turn_usage(self.trace_id)
        if usage:
            data["usage"] = usage
        if extra:
            data.update(extra)
        self._write(EventType.DONE, data, "")

    def timing_metrics(self) -> Dict[str, Any]:
        return self.timing.metrics()


def to_sse(payload: Dict[str, Any]) -> str:
    """custom chunk -> SSE 行，保持 UI 解析格式不变。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
