"""记忆子图：候选记忆提取 + 审批（interrupt）。

记忆提取不占用户回答的关键路径：``MemoryExtractionRunner`` 在后台线程里
驱动一张独立的图，事件写进本地收集器，UI 通过 peek/pop 取结果。
审批复用 ``ActionApprovalGraph``（LangGraph ``interrupt`` + checkpointer）。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Optional

from langgraph.graph import END, START, StateGraph

from app.graphs.context import TurnContext
from app.graphs.state import TurnState
from app.graphs.nodes import extract_memory


def build_memory_graph(checkpointer: Optional[Any] = None):
    graph = StateGraph(TurnState, context_schema=TurnContext)
    graph.add_node("extract", extract_memory, timeout=30)
    graph.add_edge(START, "extract")
    graph.add_edge("extract", END)
    return graph.compile(checkpointer=checkpointer)


def _key(user_id: str, session_id: str, scene: str) -> tuple:
    return str(user_id), str(session_id), str(scene)


class MemoryExtractionRunner:
    """后台驱动记忆提取图，结果按 (user, session, scene) 缓存。"""

    def __init__(self, services: Any, graph=None):
        self.services = services
        self.graph = build_memory_graph()
        self._results: Dict[tuple, Dict[str, Any]] = {}
        self._events: Dict[tuple, threading.Event] = {}
        self._lock = threading.RLock()

    def start(
        self,
        user_id: str,
        session_id: str,
        scene: str,
        excluded_contents: Optional[List[str]] = None,
        messages: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        key = _key(user_id, session_id, scene)
        done = threading.Event()
        with self._lock:
            self._results.pop(key, None)
            self._events[key] = done

        def run() -> None:
            payload: Dict[str, Any]
            try:
                payload = self._run_graph(
                    user_id, session_id, scene, list(excluded_contents or []), list(messages or [])
                )
            except Exception as exc:
                payload = {
                    "candidates": [],
                    "diagnostic": f"记忆提取失败：{type(exc).__name__}: {exc}",
                    "status": "error",
                    "has_error": True,
                    "should_display": True,
                }
            with self._lock:
                self._results[key] = payload
                done.set()

        threading.Thread(target=run, name=f"memory-graph:{user_id}:{scene}", daemon=True).start()

    def _run_graph(
        self,
        user_id: str,
        session_id: str,
        scene: str,
        excluded: List[str],
        messages: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        collected: List[Dict[str, Any]] = []
        from app.graphs.events import TurnTiming
        from app.observability import new_trace_id

        ctx = TurnContext(
            services=self.services,
            timing=TurnTiming(),
            trace_id=new_trace_id(),
        ).bind_writer(collected.append)
        asyncio.run(self.graph.ainvoke(
            {
                "user_message": "",
                "user_id": user_id,
                "session_id": session_id,
                "scene": scene,
                "excluded_memories": excluded,
                "memory_window": list(messages or []),
                "status": "started",
            },
            context=ctx,
        ))
        for event in reversed(collected):
            if event.get("event") == "memory_extraction":
                return dict(event.get("data") or {})
        return {"candidates": [], "status": "no_content", "has_error": False, "should_display": False}

    def peek(self, user_id: str, session_id: str, scene: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._results.get(_key(user_id, session_id, scene))

    def pop(self, user_id: str, session_id: str, scene: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._results.pop(_key(user_id, session_id, scene), None)

    def is_pending(self, user_id: str, session_id: str, scene: str) -> bool:
        key = _key(user_id, session_id, scene)
        with self._lock:
            done = self._events.get(key)
            return done is not None and not done.is_set()

    def wait(self, user_id: str, session_id: str, scene: str, timeout: Optional[float] = None):
        key = _key(user_id, session_id, scene)
        with self._lock:
            done = self._events.get(key)
        if done is None:
            return self.peek(user_id, session_id, scene)
        if not done.wait(timeout):
            return None
        return self.peek(user_id, session_id, scene)


class MemoryCenter:
    """记忆审批入口；底层是带 interrupt 的 LangGraph 图。"""

    def __init__(self, services: Any):
        self.services = services

    def start_approval(self, user_id: str, session_id: str, scene: str, candidates: List[Dict[str, Any]]):
        return self.services.start_memory_approval(user_id, session_id, scene, candidates)

    def resume_approval(self, approval_id: str, decision: Dict[str, Any]):
        return self.services.resume_memory_approval(approval_id, decision)
