"""
Stream Renderer - 在 Streamlit 中实时渲染 streaming 事件

核心机制：
1. 后台线程运行 async 生成器，事件推入线程安全队列
2. 主线程轮询队列，实时写入 Streamlit 容器
3. token 事件直接写入 write_stream 回调
"""
import asyncio
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import streamlit as st


def render_streaming_realtime(
    async_gen_factory: Callable,
    *,
    on_thinking: Callable[[str], None] = None,
    on_classify: Callable[[dict], None] = None,
    on_step: Callable[[dict], None] = None,
    on_tool_call: Callable[[str, str], None] = None,
    on_tool_result: Callable[[str, str], None] = None,
    on_token: Callable[[str], None] = None,
    on_error: Callable[[str], None] = None,
    on_done: Callable[[dict], None] = None,
) -> str:
    """
    实时渲染 streaming 事件流

    Args:
        async_gen_factory: 返回 async generator 的工厂函数
        on_*: 各类事件的回调，直接操作 Streamlit 容器

    Returns:
        str: 完整的响应文本
    """
    import queue as thread_queue

    event_q: thread_queue.Queue = thread_queue.Queue()
    token_buffer: List[str] = []
    stop_event = threading.Event()

    # 后台线程：运行 async 生成器
    def _bg_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _drain():
            try:
                gen = async_gen_factory()
                async for sse_line in gen:
                    if stop_event.is_set():
                        break
                    if sse_line.startswith("data: "):
                        try:
                            evt = json.loads(sse_line[6:])
                            event_q.put(evt)
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                event_q.put({"event": "error", "data": {"message": str(e)}})
            finally:
                event_q.put(None)  # 结束信号

        loop.run_until_complete(_drain())
        loop.close()

    bg = threading.Thread(target=_bg_worker, daemon=True)
    bg.start()

    # 主线程：轮询队列并渲染
    try:
        while True:
            try:
                evt = event_q.get(timeout=0.1)
            except thread_queue.Empty:
                continue

            if evt is None:
                break

            event_type = evt.get("event", "")
            data = evt.get("data", {})

            if event_type == "thinking" and on_thinking:
                on_thinking(data.get("message", ""))

            elif event_type == "classify" and on_classify:
                on_classify(data)

            elif event_type == "step" and on_step:
                on_step(data)

            elif event_type == "tool_call" and on_tool_call:
                on_tool_call(data.get("tool", "?"), data.get("input", ""))

            elif event_type == "tool_result" and on_tool_result:
                on_tool_result(data.get("tool", "?"), data.get("output", ""))

            elif event_type == "token" and on_token:
                token = data.get("token", "")
                token_buffer.append(token)
                on_token(token)

            elif event_type == "error" and on_error:
                on_error(data.get("message", "未知错误"))

            elif event_type == "done" and on_done:
                on_done(data)

            elif event_type == "retrieval":
                pass  # 由 tool_call/tool_result 覆盖

    finally:
        stop_event.set()
        bg.join(timeout=2)

    return "".join(token_buffer)
