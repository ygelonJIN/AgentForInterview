"""关键同步阻塞调用移出事件循环的回归测试。"""

import asyncio
import threading

from app.graphs.context import TurnContext
from app.graphs.nodes import extract_memory
from app.services.turn_services import TurnServices


class _Queue:
    def __init__(self):
        self.events = []

    async def emit(self, event_type, data, step=""):
        self.events.append((event_type, data, step))

    async def emit_tool_call(self, tool, detail, step=""):
        self.events.append(("tool_call", {"tool": tool, "input": detail}, step))


class _Runtime:
    """节点只用到 runtime.context 与 runtime.stream_writer。"""

    def __init__(self, context, writer):
        self.context = context
        self.stream_writer = writer


def test_retrieval_runs_outside_event_loop_thread():
    class _Service:
        def __init__(self):
            self.thread_id = None

        def retrieve(self, query, **kwargs):
            self.thread_id = threading.get_ident()
            return {
                "products": [],
                "documents": [],
                "diagnostics": [],
                "rag_ok": None,
                "rag_error": None,
                "strategy": kwargs["strategy"],
                "needs": kwargs.get("needs") or {},
            }

    services = TurnServices.__new__(TurnServices)
    services._retrieval_service = _Service()
    queue = _Queue()
    caller_thread = threading.get_ident()

    result = asyncio.run(services._smart_retrieve(
        "查询",
        "sql_only",
        queue,
        needs={"needs_product": True, "needs_review": False},
    ))

    assert result["strategy"] == "sql_only"
    assert services._retrieval_service.thread_id not in {None, caller_thread}


def test_memory_extraction_runs_outside_event_loop_thread():
    class _Result:
        def as_dict(self):
            return {"candidates": [], "status": "no_content"}

    class _Services:
        def __init__(self):
            self.thread_id = None

        def extract_candidate_memories_result(self, *args, **kwargs):
            self.thread_id = threading.get_ident()
            return _Result()

    services = _Services()
    collected = []
    ctx = TurnContext(services=services, trace_id="mem")
    runtime = _Runtime(ctx, collected.append)
    caller_thread = threading.get_ident()

    result = asyncio.run(extract_memory(
        {
            "user_id": "user-1",
            "session_id": "browser-1",
            "scene": "general",
            "excluded_memories": [],
        },
        runtime,
    ))

    assert result["memory_extraction"]["status"] == "no_content"
    assert services.thread_id not in {None, caller_thread}
    assert any(event["event"] == "memory_extraction" for event in collected)
