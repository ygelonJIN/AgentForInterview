"""
Streaming 事件系统 - 全链路实时推送
替换所有 invoke() 阻塞调用为 astream() 流式调用
"""
import asyncio
import json
import time
from typing import AsyncGenerator, Dict, Any, Optional, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum


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
    STEP = "step"                  # 步骤进度
    ERROR = "error"                # 错误
    DONE = "done"                  # 完成


@dataclass
class StreamEvent:
    """流式事件"""
    event: str
    data: Dict[str, Any]
    step: str = ""
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_sse(self) -> str:
        """转换为 SSE 格式"""
        payload = {
            "event": self.event,
            "data": self.data,
            "step": self.step,
            "timestamp": self.timestamp
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class EventQueue:
    """事件队列 - 生产者/消费者模式"""

    def __init__(self):
        self._queue: asyncio.Queue[Optional[StreamEvent]] = asyncio.Queue()
        self._start_time = time.time()

    async def emit(self, event_type: str, data: Dict[str, Any], step: str = ""):
        """推送事件"""
        event = StreamEvent(
            event=event_type,
            data=data,
            step=step
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
