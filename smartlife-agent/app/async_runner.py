"""进程级持久事件循环，避免异步客户端跨已关闭 loop 复用。"""
import asyncio
import threading
from typing import Coroutine, Any


class SharedAsyncRunner:
    """所有流式任务共享同一个常驻事件循环。"""

    _lock = threading.Lock()
    _loop = None
    _thread = None

    @classmethod
    def _ensure_loop(cls):
        with cls._lock:
            if cls._loop is not None and not cls._loop.is_closed():
                return cls._loop

            loop = asyncio.new_event_loop()

            def _run_loop():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = threading.Thread(
                target=_run_loop,
                name="smartlife-shared-async-loop",
                daemon=True,
            )
            thread.start()
            cls._loop = loop
            cls._thread = thread
            return loop

    @classmethod
    def submit(cls, coroutine: Coroutine[Any, Any, Any]):
        return asyncio.run_coroutine_threadsafe(coroutine, cls._ensure_loop())
