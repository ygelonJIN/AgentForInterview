"""
Streaming 事件系统 - 全链路实时推送
替换所有 invoke() 阻塞调用为 astream() 流式调用
"""
import asyncio
import json
import time
from typing import AsyncGenerator, Dict, Any, Callable, List, Optional
from dataclasses import dataclass, field, asdict
from enum import Enum

from app.observability import new_trace_id


def _event_type_value(event_type: Any) -> str:
    return str(getattr(event_type, "value", event_type))


class EventType(str, Enum):
    """事件类型枚举"""
    THINKING = "thinking"          # 模型正在思考
    CLASSIFY = "classify"          # 分类结果（路由+意图合并）
    ROUTE = "route"                # 路由结果（兼容旧接口）
    INTENT = "intent"              # 意图识别结果
    TOOL_CALL = "tool_call"        # 工具调用开始
    TOOL_RESULT = "tool_result"    # 工具返回结果
    RETRIEVAL = "retrieval"        # 检索结果
    TOKEN = "token"                # 流式 token（打字机效果）
    RESPONSE_RESET = "response_reset"  # 新版本正文替换旧版本正文
    STEP = "step"                  # 步骤进度
    EXECUTION_LOG = "execution_log"  # 分支、重试、循环、降级日志
    ERROR = "error"                # 错误
    DONE = "done"                  # 完成


@dataclass
class StreamEvent:
    """流式事件"""
    event: str
    data: Dict[str, Any]
    step: str = ""
    timestamp: float = 0.0
    trace_id: str = ""

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_sse(self) -> str:
        """转换为 SSE 格式"""
        payload = {
            "event": self.event,
            "data": self.data,
            "step": self.step,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class EventQueue:
    """事件队列 - 生产者/消费者模式"""

    def __init__(self, trace_id: Optional[str] = None):
        self._queue: asyncio.Queue[Optional[StreamEvent]] = asyncio.Queue()
        self._start_time = time.time()
        self.trace_id = trace_id or new_trace_id()

    async def emit(self, event_type: str, data: Dict[str, Any], step: str = ""):
        """推送事件"""
        event = StreamEvent(
            event=_event_type_value(event_type),
            data=data,
            step=step,
            trace_id=self.trace_id,
        )
        await self._queue.put(event)

    async def emit_thinking(self, message: str, step: str = "", model: str = ""):
        """推送思考事件"""
        data = {"message": message}
        if model:
            data["model"] = model
        await self.emit(EventType.THINKING, data, step)

    async def emit_token(self, token: str, step: str = ""):
        """推送流式 token"""
        await self.emit(EventType.TOKEN, {"token": token}, step)

    async def emit_response_reset(self, step: str = ""):
        """通知 UI 清空当前流式正文，后续 token 是替换版本。"""
        await self.emit(EventType.RESPONSE_RESET, {}, step)

    async def emit_tool_call(self, tool_name: str, tool_input: Any = None, step: str = ""):
        """推送工具调用"""
        data = {"tool": tool_name}
        if tool_input is not None:
            data["input"] = str(tool_input)[:500]
        await self.emit(EventType.TOOL_CALL, data, step)

    async def emit_tool_result(self, tool_name: str, output: str, step: str = ""):
        """推送工具结果"""
        await self.emit(EventType.TOOL_RESULT, {
            "tool": tool_name,
            "output": output[:1000]
        }, step)

    async def emit_execution_log(self, payload: Dict[str, Any], step: str = ""):
        """推送可见执行日志。"""
        data = dict(payload.get("data") or {})
        await self.emit(
            payload.get("event", EventType.EXECUTION_LOG),
            data,
            payload.get("step", step),
        )

    async def emit_error(self, message: str, step: str = ""):
        """推送错误"""
        await self.emit(EventType.ERROR, {"message": message}, step)

    async def emit_done(self, extra: Dict[str, Any] = None):
        """推送完成"""
        data = {"total_time": f"{time.time() - self._start_time:.1f}s"}
        if extra:
            data.update(extra)
        await self.emit(EventType.DONE, data)

    async def finish(self):
        """标记队列结束"""
        await self._queue.put(None)

    async def __aiter__(self) -> AsyncGenerator[StreamEvent, None]:
        """异步迭代器，逐个返回事件"""
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event

    async def to_sse(self) -> AsyncGenerator[str, None]:
        """转换为 SSE 流"""
        async for event in self:
            yield event.to_sse()


def deduplicate_process_events(events: Any) -> List[Dict[str, Any]]:
    """按逻辑执行节点去重，同时保留该节点最后一次状态。

    流式 UI 会随每个事件刷新整块过程区，后端重试或重复事件不应显示成两次执行。
    Step 按步骤编号去重，分类结果按分类节点去重，工具事件只合并完全相同的调用。
    """
    ordered_keys: List[Any] = []
    by_key: Dict[Any, Dict[str, Any]] = {}

    for index, event in enumerate(events or []):
        if not isinstance(event, dict):
            continue
        event_type = _event_type_value(event.get("event", ""))
        data = event.get("data") or {}
        stream_step = event.get("step", "")

        if event_type == EventType.STEP:
            logical_step = data.get("step") or data.get("name") or stream_step
            key = ("step", logical_step)
        elif event_type == EventType.CLASSIFY:
            key = ("classify", stream_step or "classify")
        elif event_type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
            payload_key = "input" if event_type == EventType.TOOL_CALL else "output"
            key = (
                event_type,
                stream_step,
                data.get("tool", ""),
                payload_key,
                data.get(payload_key, ""),
            )
        else:
            key = ("unique", index)

        if key not in by_key:
            ordered_keys.append(key)
        by_key[key] = event

    return [by_key[key] for key in ordered_keys]


def reconcile_tool_events(
    events: Any,
    *,
    completed: bool = False,
) -> List[Dict[str, Any]]:
    """合并工具调用与结果，避免调用卡片永久停留在“正在执行”。

    同一工具可能连续执行多次，因此按 ``(tool, step)`` 的出现顺序配对。
    流式阶段保留未返回结果的 active 状态；当整个过程已完成时，即使旧事件流缺少
    ``tool_result``，也把遗留调用收尾为 completed。
    """
    reconciled: List[Optional[Dict[str, Any]]] = []
    pending: Dict[Any, List[int]] = {}

    for index, event in enumerate(events or []):
        if not isinstance(event, dict):
            continue
        normalized = dict(event)
        normalized["data"] = dict(event.get("data") or {})
        event_type = _event_type_value(normalized.get("event", ""))
        data = normalized["data"]
        key = (data.get("tool", ""), event.get("step", ""))

        if event_type == EventType.TOOL_CALL:
            pending.setdefault(key, []).append(len(reconciled))
            reconciled.append(normalized)
            continue

        if event_type == EventType.TOOL_RESULT:
            call_indexes = pending.get(key) or []
            if call_indexes:
                call_index = call_indexes.pop(0)
                call = reconciled[call_index]
                call["data"]["status"] = "completed"
                call["data"]["output"] = data.get("output", "")
            reconciled.append(normalized)
            continue

        reconciled.append(normalized)

    if completed:
        for call_indexes in pending.values():
            for call_index in call_indexes:
                reconciled[call_index]["data"]["status"] = "completed"

    return [event for event in reconciled if event is not None]


class StreamingLLMCallback:
    """
    LangChain 兼容的流式回调包装器
    将 LLM 的 stream 输出转发到 EventQueue
    """

    def __init__(self, queue: EventQueue, step: str = "llm"):
        self.queue = queue
        self.step = step

    async def on_token(self, token: str):
        """每个 token 触发"""
        await self.queue.emit_token(token, self.step)

    async def on_start(self, model: str = ""):
        """LLM 开始调用"""
        await self.queue.emit_thinking(
            f"🧠 模型 {model} 正在思考..." if model else "🧠 模型正在思考...",
            step=self.step,
            model=model
        )

    async def on_end(self, full_text: str):
        """LLM 调用结束"""
        pass  # 由上层处理

    async def on_error(self, error: str):
        """LLM 调用出错"""
        await self.queue.emit_error(error, self.step)


def parse_sse_line(line: str) -> Optional[Dict[str, Any]]:
    """解析 SSE 行"""
    if line.startswith("data: "):
        try:
            return json.loads(line[6:])
        except json.JSONDecodeError:
            return None
    return None
