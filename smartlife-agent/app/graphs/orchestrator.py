"""顶层图：唯一控制流入口。

节点组合方式：
- 单场景 → 编译子图直接作为节点（custom 事件原生透传）
- 复合意图 → 并行分支 + ``branch_results`` reducer 归约
- 收束 → ``finalize`` 发 done；记忆提取作为独立图，不占关键路径

没有任何编排对象被 UI 直接调用；流式走 ``astream(stream_mode="custom")``。
"""
from __future__ import annotations

import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.graphs.context import TurnContext, build_turn_context
from app.graphs.events import to_sse
from app.graphs.memory import MemoryExtractionRunner, MemoryCenter
from app.graphs.nodes import classify, extract_memory, finalize, prepare, route
from app.graphs.state import TurnState
from app.graphs.subgraphs.composite import build_composite_graph
from app.graphs.subgraphs.customer_service import build_customer_service_graph
from app.graphs.subgraphs.general_chat import build_general_chat_graph
from app.graphs.subgraphs.general_plan import build_general_plan_graph
from app.graphs.subgraphs.react import build_react_graph
from app.graphs.subgraphs.shopping import build_shopping_graph
from app.graphs.subgraphs.travel import build_travel_graph
from app.graphs.subgraphs.trip_gear import build_trip_gear_graph
from app.observability import log_exception, new_trace_id, timed_span
from app.session import build_thread_id

ROUTE_TARGETS = {
    "shopping": "shopping",
    "travel": "travel",
    "trip_gear": "trip_gear",
    "composite": "composite",
    "customer_service": "customer_service",
    "general_plan": "general_plan",
    "react": "react",
    "general_chat": "general_chat",
}


def build_turn_graph(checkpointer: Optional[Any] = None):
    """组装顶层状态图；子图作为节点直接挂载。"""
    graph = StateGraph(TurnState, context_schema=TurnContext)
    graph.add_node("prepare", prepare, timeout=5)
    graph.add_node("classify", classify, timeout=20)
    graph.add_node("shopping", build_shopping_graph())
    graph.add_node("travel", build_travel_graph())
    graph.add_node("trip_gear", build_trip_gear_graph())
    graph.add_node("composite", build_composite_graph())
    graph.add_node("customer_service", build_customer_service_graph())
    graph.add_node("general_plan", build_general_plan_graph())
    graph.add_node("react", build_react_graph())
    graph.add_node("general_chat", build_general_chat_graph())
    graph.add_node("finalize", finalize, timeout=10)
    graph.add_node("extract_memory", extract_memory, timeout=30)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "classify")
    graph.add_conditional_edges("classify", route, ROUTE_TARGETS)
    for scene_node in ROUTE_TARGETS.values():
        graph.add_edge(scene_node, "finalize")
    # 记忆提取图挂在 finalize 之后，由入口按需触发（默认不进关键路径）。
    graph.add_conditional_edges(
        "finalize",
        _maybe_extract_memory,
        {"extract_memory": "extract_memory", "done": END},
    )
    graph.add_edge("extract_memory", END)
    return graph.compile(checkpointer=checkpointer)


def _maybe_extract_memory(state: TurnState) -> str:
    return "extract_memory" if state.get("extract_memory") else "done"


class TurnGraph:
    """对外入口：驱动图、转发事件、承载记忆中心操作。"""

    def __init__(self, services: Any = None, checkpointer: Optional[Any] = None):
        from app.services.turn_services import get_turn_services

        self.services = services or get_turn_services()
        self.checkpointer = checkpointer
        self.graph = build_turn_graph(checkpointer)
        self._memory_runner = MemoryExtractionRunner(self.services, self.graph)
        self._memory_center = MemoryCenter(self.services)

    # ---- 主链路 ----
    async def process_streaming(
        self,
        user_message: str,
        user_id: str,
        session_id: str,
        scene: str = "general",
        excluded_memories: Optional[List[str]] = None,
    ) -> AsyncGenerator[str, None]:
        thread_id = build_thread_id(user_id, scene, session_id)
        trace_id = new_trace_id()
        context = build_turn_context(self.services, trace_id).with_extra(
            actor_id=user_id, agent_id="general"
        )
        payload = {
            "user_message": user_message,
            "user_id": user_id,
            "session_id": session_id,
            "scene": scene,
            "excluded_memories": list(excluded_memories or []),
            "status": "started",
        }
        config = {"configurable": {"thread_id": thread_id}}

        async for chunk in self._stream_graph(payload, config, context, trace_id):
            yield chunk

    async def resume_streaming(
        self,
        answer: Any,
        user_id: str,
        session_id: str,
        scene: str = "general",
    ) -> AsyncGenerator[str, None]:
        """用户补充信息后恢复被中断的一轮。

        必须用与中断时完全相同的 thread_id 才能命中检查点，否则会另起一轮。
        """
        thread_id = build_thread_id(user_id, scene, session_id)
        trace_id = new_trace_id()
        context = build_turn_context(self.services, trace_id).with_extra(
            actor_id=user_id, agent_id="general"
        )
        config = {"configurable": {"thread_id": thread_id}}
        async for chunk in self._stream_graph(Command(resume=answer), config, context, trace_id):
            yield chunk

    async def _stream_graph(
        self,
        graph_input: Any,
        config: Dict[str, Any],
        context: Any,
        trace_id: str,
    ) -> AsyncGenerator[str, None]:
        """统一的流式执行：业务事件、节点计时、中断识别与错误兜底。"""
        thread_id = config.get("configurable", {}).get("thread_id", "")
        task_starts: Dict[str, float] = {}
        try:
            with timed_span("graph.turn", trace_id=trace_id, attributes={"thread_id": thread_id}):
                async for chunk in self.graph.astream(
                    graph_input,
                    config=config,
                    context=context,
                    # custom：业务事件；tasks：节点生命周期；updates：识别中断。
                    stream_mode=["custom", "tasks", "updates"],
                    subgraphs=True,
                ):
                    namespace, mode, body = _split_chunk(chunk)
                    if mode == "custom":
                        if isinstance(body, dict):
                            yield to_sse(body)
                        continue
                    if mode == "updates":
                        payload = _interrupt_payload(body)
                        if payload is not None:
                            # 子图和父图会各报一次，用 namespace 去重：只认父图那一层
                            if namespace:
                                continue
                            yield to_sse({
                                "event": "clarify",
                                "data": {**payload, "thread_id": thread_id},
                                "step": "clarify",
                                "timestamp": time.time(),
                                "trace_id": trace_id,
                            })
                            return
                        continue
                    if mode == "tasks":
                        log_event = _task_log(body, task_starts, trace_id)
                        if log_event is not None:
                            yield to_sse(log_event)
        except Exception as exc:
            log_exception("graph.process_streaming", exc, {"thread_id": thread_id})
            yield to_sse({
                "event": "error",
                "data": {"message": f"处理失败: {type(exc).__name__}: {exc or '(无错误信息)'}"},
                "step": "graph",
                "timestamp": time.time(),
                "trace_id": trace_id,
            })
            yield to_sse({
                "event": "done",
                "data": {
                    "response": "",
                    "timing_ms": context.timing.metrics(),
                    "total_time": "0.0s",
                },
                "step": "",
                "timestamp": time.time(),
                "trace_id": trace_id,
            })

    # ---- 记忆中心：同样由图承担，不直连领域对象 ----
    def start_memory_approval(self, user_id: str, session_id: str, scene: str, candidates: List[Dict[str, Any]]):
        return self._memory_center.start_approval(user_id, session_id, scene, candidates)

    def resume_memory_approval(self, approval_id: str, decision: Dict[str, Any]):
        return self._memory_center.resume_approval(approval_id, decision)

    def peek_memory_extraction_result(self, user_id: str, session_id: str, scene: str):
        return self._memory_runner.peek(user_id, session_id, scene)

    def pop_memory_extraction_result(self, user_id: str, session_id: str, scene: str):
        return self._memory_runner.pop(user_id, session_id, scene)

    def is_memory_extraction_pending(self, user_id: str, session_id: str, scene: str) -> bool:
        return self._memory_runner.is_pending(user_id, session_id, scene)

    def wait_for_memory_extraction(self, user_id: str, session_id: str, scene: str, timeout: Optional[float] = None):
        return self._memory_runner.wait(user_id, session_id, scene, timeout)

    def extract_candidate_memories_result(self, user_id: str, session_id: str, scene: str, excluded_contents=None):
        return self.services.extract_candidate_memories_result(
            user_id, session_id, scene, excluded_contents=excluded_contents or []
        )


def _interrupt_payload(body: Any) -> Optional[Dict[str, Any]]:
    """从 updates 分块里取出中断载荷；不是中断就返回 None。"""
    if not isinstance(body, dict):
        return None
    interrupts = body.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0] if isinstance(interrupts, (list, tuple)) else interrupts
    value = getattr(first, "value", first)
    if isinstance(value, dict):
        return value
    return {"type": "interrupt", "question": str(value)}


def _split_chunk(chunk: Any):
    """把 ``astream(stream_mode=[...], subgraphs=True)`` 的产出统一成三元组。"""
    if isinstance(chunk, tuple):
        if len(chunk) == 3:
            namespace, mode, body = chunk
            return namespace, mode, body
        if len(chunk) == 2:
            namespace, body = chunk
            return namespace, "custom", body
        return (), "custom", None
    return (), "custom", chunk


def _task_log(
    body: Any,
    starts: Dict[str, float],
    trace_id: str,
) -> Optional[Dict[str, Any]]:
    """把 LangGraph tasks 流翻译成 UI 认识的 execution_log。"""
    if not isinstance(body, dict):
        return None
    task_id = str(body.get("id") or "")
    node = str(body.get("name") or "")
    if not task_id or not node:
        return None
    now = time.monotonic()
    if "result" not in body and "error" not in body:
        starts[task_id] = now
        kind, status = "node_started", "running"
        message = f"开始执行 {node}"
        duration_ms = None
    else:
        started = starts.pop(task_id, None)
        duration_ms = round((now - started) * 1000, 3) if started else None
        failed = body.get("error") is not None
        kind = "node_failed" if failed else "node_succeeded"
        status = "error" if failed else "ok"
        message = f"{node} 执行失败" if failed else f"{node} 执行完成"
    return {
        "event": "execution_log",
        "data": {
            "graph": "langgraph",
            "node": node,
            "kind": kind,
            "status": status,
            "message": message,
            "duration_ms": duration_ms,
            "details": {},
        },
        "step": node,
        "timestamp": time.time(),
        "trace_id": trace_id,
    }


_turn_graph: Optional[TurnGraph] = None


def get_turn_graph(model_name: Optional[str] = None) -> TurnGraph:
    global _turn_graph
    if _turn_graph is None:
        from app.services.turn_services import TurnServices

        # 中断/恢复依赖检查点：没有 checkpointer 时 Command(resume=...) 直接报错。
        try:
            from app.checkpointing import create_checkpointer

            checkpointer = create_checkpointer()
        except Exception as exc:  # 持久化不可用不阻塞对话，只是无法恢复中断
            log_exception("graph.checkpointer", exc)
            checkpointer = None
        _turn_graph = TurnGraph(TurnServices(model_name), checkpointer=checkpointer)
    return _turn_graph
