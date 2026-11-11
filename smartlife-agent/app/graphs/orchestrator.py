"""顶层图：唯一控制流入口。

节点组合方式：
- 单场景 → 编译子图直接作为节点（custom 事件原生透传）
- 收束 → ``finalize`` 发 done；记忆提取作为独立图，不占关键路径
- 复合请求不再并行：shopping + travel 的"买东西去玩"由 trip_gear 一次算完

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
from app.graphs.nodes import classify, finalize, prepare, route
from app.graphs.state import TurnState
from app.agents.specs import iter_specs
from app.observability import log_exception, new_trace_id, timed_span, trace_context, turn_usage
from app.session import build_thread_id

# 五个 Agent = 五个节点 = 五条工具白名单。
# 清单唯一来源是 app/agents/specs.py：加 Agent 只改那里 + 一条工具白名单。
# travel 与 trip_gear 共用一套图，差别只有 with_gear。
def route_targets() -> Dict[str, str]:
    """节点 → 节点；导入的 Agent 也会出现在这里，所以每次建图时重新算。"""
    return {spec.agent_id: spec.agent_id for spec in iter_specs()}


ROUTE_TARGETS = route_targets()


# 场景 → Agent 能力面。原来这里写死 "general"，导致 shopping / travel 两个
# 能力面在运行时永远绑不上，只有点名调用和测试用得上。
_AGENT_SCENES = {"shopping", "travel", "general"}


def _agent_id_for_scene(scene: str) -> str:
    value = str(scene or "").strip().lower()
    return value if value in _AGENT_SCENES else "general"


def build_turn_graph(checkpointer: Optional[Any] = None):
    """组装顶层状态图；子图作为节点直接挂载。"""
    graph = StateGraph(TurnState, context_schema=TurnContext)
    graph.add_node("prepare", prepare, timeout=5)
    graph.add_node("classify", classify, timeout=20)
    for spec in iter_specs():
        graph.add_node(spec.agent_id, spec.build())
    graph.add_node("finalize", finalize, timeout=10)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "classify")
    targets = route_targets()
    graph.add_conditional_edges("classify", route, targets)
    for spec in iter_specs():
        graph.add_edge(spec.agent_id, "finalize")
    # 记忆提取不进关键路径：由 UI 在流结束后单独跑 MemoryExtractionRunner，
    # 原来那条"state["extract_memory"] 为真才走"的分支从来没有被置真过。
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


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
            actor_id=user_id, agent_id=_agent_id_for_scene(scene)
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

    async def invoke(
        self,
        user_message: str,
        user_id: str,
        session_id: str,
        scene: str = "general",
        agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """非流式一次调用（脚本/评测用）；``agent`` 可点名入口。"""
        thread_id = build_thread_id(user_id, scene, session_id)
        trace_id = new_trace_id()
        context = build_turn_context(self.services, trace_id).with_extra(
            actor_id=user_id, agent_id=agent or _agent_id_for_scene(scene)
        )
        payload = {
            "user_message": user_message,
            "user_id": user_id,
            "session_id": session_id,
            "scene": scene,
            "status": "started",
        }
        if agent:
            payload["forced_agent"] = agent
        config = {"configurable": {"thread_id": thread_id}}
        return await self.graph.ainvoke(payload, config=config, context=context)

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
            actor_id=user_id, agent_id=_agent_id_for_scene(scene)
        )
        # 补答本身也是用户消息：图从 interrupt 处继续，prepare 不会重跑，
        # 不在这里补记一笔，库里就永远缺这句，内存顺序和库顺序会对不上，
        # 随后每次重跑都会被"旧消息迁移"当成新消息再写一遍。
        if isinstance(answer, str) and answer.strip():
            await self.services.begin_turn(answer, thread_id)
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
            with trace_context(trace_id), timed_span(
                "graph.turn", trace_id=trace_id, attributes={"thread_id": thread_id}
            ):
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
            error_done = {
                "response": "",
                "timing_ms": context.timing.metrics(),
                "total_time": "0.0s",
            }
            usage = turn_usage(trace_id)
            if usage:
                error_done["usage"] = usage
            yield to_sse({
                "event": "done",
                "data": error_done,
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

    def start_memory_extraction(
        self,
        user_id: str,
        session_id: str,
        scene: str,
        excluded_contents=None,
        messages=None,
    ) -> None:
        """在后台线程里跑记忆子图。

        主图已经不再挂 ``extract_memory`` 节点（那条分支从未被置真），候选记忆
        只能由「提取记忆」按钮主动起一次。``messages`` 是 UI 按游标切好的窗口，
        不传就退回"本会话最后一条用户消息"。
        """
        self._memory_runner.start(user_id, session_id, scene, excluded_contents or [], messages)

    def memory_extraction_window(self, user_id: str, session_id: str, scene: str):
        """游标之后还没提取过的用户消息；空表示没有新内容，不用调模型。"""
        return self.services.memory_extraction_window(user_id, session_id, scene)

    def record_memory_extraction(self, thread_id: str, last_message_id: int) -> None:
        self.services.record_memory_extraction(thread_id, last_message_id)

    def save_memory_candidates(self, user_id: str, candidates, selected_indices=None):
        """候选记忆写入：按钮点击本身就是确认，不再起 interrupt 审批。

        与 ``MemoryApprovalGraph`` 用的是同一个持久化入口
        （``services.save_user_selected_memories``），只是省掉一次图往返。
        """
        indices = (
            list(range(len(candidates)))
            if selected_indices is None
            else [int(index) for index in selected_indices]
        )
        return self.services.save_user_selected_memories(user_id, list(candidates), indices)

    def delete_memory(self, user_id: str, memory_id: str) -> bool:
        """撤销一条刚写入的记忆：MD 与向量一起删。"""
        return bool(self.services.memory_repository.delete_memory(user_id, memory_id))


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
