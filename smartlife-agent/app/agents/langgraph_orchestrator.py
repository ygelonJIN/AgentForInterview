"""
LangGraph 顶层编排器。

顶层图负责分类、条件路由、场景执行和最终状态提交。
领域实现继续由 OrchestratorV2 的服务方法承担，避免重写 SQL/RAG/模型/工具。
"""
import asyncio
import inspect
import threading
from typing import Any, AsyncGenerator, Dict, List, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agents.orchestrator_v2 import OrchestratorV2
from app.checkpointing import create_checkpointer
from app.classifier import ClassificationResult, UnifiedClassifier
from app.observability import timed_span
from app.reliability import NodePolicy, RetryPolicy, RetryableError
from app.session import build_thread_id
from app.execution_log import emit_execution_log, run_policy_node
from app.streaming import EventQueue


class _CompositeEventQueue:
    """把并行子图的正文隔离到固定分区，实时输出且不互相插队。"""

    def __init__(self, delegate: EventQueue, section: str):
        self._delegate = delegate
        self.section = section
        self.filtered_tokens: List[str] = []

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    @staticmethod
    def _event_name(event_type: Any) -> str:
        return str(getattr(event_type, "value", event_type))

    async def emit(self, event_type: Any, data: Dict[str, Any], step: str = ""):
        event_name = self._event_name(event_type)
        if event_name == "response_reset":
            await self.emit_response_reset(step=step)
            return
        if event_name == "token":
            await self.emit_token(str((data or {}).get("token", "")), step=step)
            return
        await self._delegate.emit(event_type, data, step)

    async def emit_token(self, token: str, step: str = ""):
        token = str(token or "")
        self.filtered_tokens.append(token)
        await self._delegate.emit_token(token, step=step, section=self.section)

    async def emit_response_reset(self, step: str = ""):
        self.filtered_tokens.clear()
        await self._delegate.emit_response_reset(step=step, section=self.section)


class OrchestratorState(TypedDict, total=False):
    user_message: str
    user_id: str
    session_id: str
    scene: str
    thread_id: str
    classification: Dict[str, Any]
    response: str
    agent_used: str
    excluded_memories: List[str]
    memory_extraction: Dict[str, Any]
    status: str
    error: str


class LangGraphOrchestrator:
    """用 StateGraph 承担真正的顶层控制流。"""

    def __init__(
        self,
        legacy_orchestrator: Optional[OrchestratorV2] = None,
        checkpointer=None,
        node_policies: Optional[Dict[str, NodePolicy]] = None,
    ):
        self.legacy = legacy_orchestrator or OrchestratorV2()
        self.checkpointer = checkpointer or create_checkpointer("memory")
        self.node_policies = node_policies or self._default_node_policies()
        self._memory_results: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        self._memory_result_events: Dict[tuple[str, str, str], threading.Event] = {}
        self._memory_lock = threading.RLock()
        self.graph = self._build_graph()

    @staticmethod
    def _default_node_policies() -> Dict[str, NodePolicy]:
        transient = (RetryableError, ConnectionError, TimeoutError, asyncio.TimeoutError)
        return {
            "prepare": NodePolicy(timeout_seconds=5, retry_policy=RetryPolicy(max_attempts=1)),
            "classify": NodePolicy(
                timeout_seconds=20,
                retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.05),
                retryable_exceptions=transient,
            ),
            # 覆盖购物子图最坏路径：规划 + 检索 + 生成 + 一次校验修复。
            "shopping": NodePolicy(timeout_seconds=300, retry_policy=RetryPolicy(max_attempts=1)),
            "travel": NodePolicy(timeout_seconds=180, retry_policy=RetryPolicy(max_attempts=1)),
            "shopping_travel": NodePolicy(timeout_seconds=480, retry_policy=RetryPolicy(max_attempts=1)),
            "negotiation": NodePolicy(timeout_seconds=120, retry_policy=RetryPolicy(max_attempts=1)),
            "react": NodePolicy(timeout_seconds=120, retry_policy=RetryPolicy(max_attempts=1)),
            "general": NodePolicy(timeout_seconds=120, retry_policy=RetryPolicy(max_attempts=1)),
            "finalize": NodePolicy(timeout_seconds=10, retry_policy=RetryPolicy(max_attempts=1)),
            "extract_memory": NodePolicy(timeout_seconds=30, retry_policy=RetryPolicy(max_attempts=1)),
        }

    def _policy_node(self, name: str, node):
        policy = self.node_policies[name]
        accepts_config = "config" in inspect.signature(node).parameters

        async def run(state: OrchestratorState, config: Optional[RunnableConfig] = None):
            queue = self._queue_from(config)
            classification = state.get("classification") or {}

            async def operation(_attempt: int):
                if accepts_config:
                    return await node(state, config)
                return await node(state)

            return await run_policy_node(
                policy,
                operation,
                queue=queue,
                graph="langgraph",
                node=name,
                step=self._step_for_node(name),
                branch=name if name in {"shopping", "travel", "shopping_travel", "negotiation", "react", "general"} else None,
                details={
                    "state_status": state.get("status", ""),
                    "intent": classification.get("intent", ""),
                    "route": classification.get("route", ""),
                    "thread_id": state.get("thread_id", ""),
                },
            )

        return run

    @staticmethod
    def _step_for_node(name: str) -> str:
        return {
            "prepare": "prepare",
            "classify": "classify",
            "shopping": "shopping",
            "travel": "travel",
            "shopping_travel": "shopping",
            "negotiation": "negotiation",
            "react": "react",
            "general": "general",
            "finalize": "done",
            "extract_memory": "memory",
        }.get(name, name)

    def _build_graph(self):
        graph = StateGraph(OrchestratorState)
        graph.add_node("prepare", self._policy_node("prepare", self._prepare))
        graph.add_node("classify", self._policy_node("classify", self._classify))
        graph.add_node("shopping", self._policy_node("shopping", self._shopping))
        graph.add_node("travel", self._policy_node("travel", self._travel))
        graph.add_node("shopping_travel", self._policy_node("shopping_travel", self._shopping_travel))
        graph.add_node("negotiation", self._policy_node("negotiation", self._negotiation))
        graph.add_node("react", self._policy_node("react", self._react))
        graph.add_node("general", self._policy_node("general", self._general))
        graph.add_node("finalize", self._policy_node("finalize", self._finalize))

        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "classify")
        graph.add_conditional_edges(
            "classify",
            self._route,
            {
                "shopping": "shopping",
                "travel": "travel",
                "shopping_travel": "shopping_travel",
                "negotiation": "negotiation",
                "react": "react",
                "general": "general",
            },
        )
        for scene_node in ("shopping", "travel", "shopping_travel", "negotiation", "react", "general"):
            graph.add_edge(scene_node, "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self.checkpointer)

    @staticmethod
    def _queue_from(config: Optional[RunnableConfig]) -> EventQueue:
        queue = (config or {}).get("configurable", {}).get("event_queue")
        if queue is None:
            raise RuntimeError("缺少运行期 EventQueue")
        return queue

    async def _prepare(self, state: OrchestratorState) -> Dict[str, Any]:
        thread_id = build_thread_id(state["user_id"], state["scene"], state["session_id"])
        await self.legacy.begin_turn(state["user_message"], thread_id)
        return {"thread_id": thread_id, "status": "prepared"}

    async def _classify(
        self,
        state: OrchestratorState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        classification = await self.legacy.classify_turn(
            state["user_message"],
            state["thread_id"],
            queue,
        )
        classification = UnifiedClassifier.apply_scene_constraint(
            ClassificationResult.model_validate(classification.model_dump()),
            state.get("scene", "general"),
            state["user_message"],
        )
        return {
            "classification": classification.model_dump(),
            "agent_used": getattr(classification, "agent_label", classification.intent),
            "status": "classified",
        }

    @staticmethod
    def _route(state: OrchestratorState) -> str:
        classification = state.get("classification", {})
        intent = classification.get("intent", "general")
        route = classification.get("route", "react")
        intents = set(classification.get("intents") or [intent])
        if {"shopping", "travel"}.issubset(intents):
            return "shopping_travel"
        if intent in {"shopping", "customer_service"}:
            return "shopping"
        if intent == "travel":
            return "travel"
        if intent == "negotiation":
            return "negotiation"
        if intent == "general" and route == "react":
            return "react"
        return "general"

    @staticmethod
    def _classification(state: OrchestratorState) -> ClassificationResult:
        return ClassificationResult.model_validate(state.get("classification", {}))

    async def _run_scene(
        self,
        state: OrchestratorState,
        config: Optional[RunnableConfig],
        scene: str,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        classification = self._classification(state)
        await emit_execution_log(
            queue,
            graph="langgraph",
            node="route",
            kind="branch_selected",
            status="ok",
            step=scene,
            branch=scene,
            details={
                "intent": classification.intent,
                "route": classification.route,
                "reason": classification.reason,
            },
            message=f"条件路由选择 {scene} 分支",
        )
        runner = {
            "shopping": self.legacy.run_shopping_turn,
            "travel": self.legacy.run_travel_turn,
            "negotiation": self.legacy.run_negotiation_turn,
            "react": self.legacy.run_react_turn,
            "general": self.legacy.run_general_turn,
        }[scene]
        response = await runner(
            state["user_message"],
            state["user_id"],
            state["thread_id"],
            classification,
            queue,
        )
        return {"response": response, "agent_used": getattr(classification, "agent_label", classification.intent), "status": "generated"}

    async def _shopping(self, state: OrchestratorState, config: Optional[RunnableConfig] = None):
        return await self._run_scene(state, config, "shopping")

    async def _travel(self, state: OrchestratorState, config: Optional[RunnableConfig] = None):
        return await self._run_scene(state, config, "travel")

    async def _shopping_travel(
        self,
        state: OrchestratorState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        """明确的购物 + 旅行复合任务：两个子图都执行，再合并正文。"""
        queue = self._queue_from(config)
        classification = self._classification(state)
        await emit_execution_log(
            queue,
            graph="langgraph",
            node="shopping_travel",
            kind="branch_selected",
            status="ok",
            step="shopping",
            branch="shopping_travel",
            details={
                "intents": classification.effective_intents,
                "intent_scores": classification.intent_scores,
                "route": classification.route,
            },
            message="检测到 shopping + travel 复合意图，依次执行两个场景并合并结果",
        )

        shopping_classification = classification.model_copy(update={
            "intent": "shopping",
            "intents": ["shopping"],
            "sub_intent": "search",
            "retrieval": "mixed",
            "intent_scores": {"shopping": classification.intent_scores.get("shopping", 1.0)},
        })
        travel_classification = classification.model_copy(update={
            "intent": "travel",
            "intents": ["travel"],
            "route": "plan_and_execute",
            "sub_intent": "plan_trip",
            "retrieval": "rag_only",
            "intent_scores": {"travel": classification.intent_scores.get("travel", 1.0)},
        })

        shopping_queue = _CompositeEventQueue(queue, "shopping")
        travel_queue = _CompositeEventQueue(queue, "travel")
        shopping_response, travel_response = await asyncio.gather(
            self.legacy.run_shopping_turn(
                state["user_message"],
                state["user_id"],
                state["thread_id"],
                shopping_classification,
                shopping_queue,
            ),
            self.legacy.run_travel_turn(
                state["user_message"],
                state["user_id"],
                state["thread_id"],
                travel_classification,
                travel_queue,
            ),
        )

        shopping_response = (shopping_response or "").strip()
        travel_response = (travel_response or "").strip()
        response = (
            "【购物推荐】\n"
            f"{shopping_response}\n\n"
            "【旅行计划】\n"
            f"{travel_response}"
        ).strip()
        return {
            "response": response,
            "agent_used": "shopping+travel",
            "status": "generated",
        }

    async def _negotiation(self, state: OrchestratorState, config: Optional[RunnableConfig] = None):
        return await self._run_scene(state, config, "negotiation")

    async def _react(self, state: OrchestratorState, config: Optional[RunnableConfig] = None):
        return await self._run_scene(state, config, "react")

    async def _general(self, state: OrchestratorState, config: Optional[RunnableConfig] = None):
        return await self._run_scene(state, config, "general")

    async def _finalize(
        self,
        state: OrchestratorState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        await self.legacy.finish_turn(
            state["thread_id"],
            state.get("response", ""),
            self._classification(state),
            queue,
        )
        self._start_memory_extraction_background(state)
        return {"status": "completed"}

    @staticmethod
    def _memory_key(user_id: str, session_id: str, scene: str) -> tuple[str, str, str]:
        return str(user_id), str(session_id), str(scene)

    def _start_memory_extraction_background(self, state: OrchestratorState) -> None:
        """回答完成后异步提取候选记忆，不再占用用户响应关键路径。"""
        key = self._memory_key(
            state.get("user_id", ""),
            state.get("session_id", ""),
            state.get("scene", "general"),
        )
        done_event = threading.Event()
        with self._memory_lock:
            self._memory_results.pop(key, None)
            self._memory_result_events[key] = done_event

        excluded_contents = list(state.get("excluded_memories") or [])

        def extract() -> None:
            try:
                result = self.legacy.extract_candidate_memories_result(
                    key[0],
                    key[1],
                    key[2],
                    excluded_contents=excluded_contents,
                )
                payload = result.as_dict()
            except Exception as exc:
                payload = {
                    "candidates": [],
                    "diagnostic": f"记忆提取失败：{type(exc).__name__}: {exc}",
                    "status": "error",
                    "has_error": True,
                    "should_display": True,
                }
            with self._memory_lock:
                self._memory_results[key] = payload
                done_event.set()

        threading.Thread(
            target=extract,
            name=f"memory-extract:{key[0]}:{key[2]}",
            daemon=True,
        ).start()

    def peek_memory_extraction_result(
        self,
        user_id: str,
        session_id: str,
        scene: str,
    ) -> Optional[Dict[str, Any]]:
        key = self._memory_key(user_id, session_id, scene)
        with self._memory_lock:
            return self._memory_results.get(key)

    def pop_memory_extraction_result(
        self,
        user_id: str,
        session_id: str,
        scene: str,
    ) -> Optional[Dict[str, Any]]:
        key = self._memory_key(user_id, session_id, scene)
        with self._memory_lock:
            return self._memory_results.pop(key, None)

    def is_memory_extraction_pending(
        self,
        user_id: str,
        session_id: str,
        scene: str,
    ) -> bool:
        key = self._memory_key(user_id, session_id, scene)
        with self._memory_lock:
            return (
                key in self._memory_result_events
                and not self._memory_result_events[key].is_set()
            )

    def wait_for_memory_extraction(
        self,
        user_id: str,
        session_id: str,
        scene: str,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        key = self._memory_key(user_id, session_id, scene)
        with self._memory_lock:
            done_event = self._memory_result_events.get(key)
        if done_event is None:
            return self.peek_memory_extraction_result(user_id, session_id, scene)
        if not done_event.wait(timeout):
            return None
        return self.peek_memory_extraction_result(user_id, session_id, scene)

    async def _extract_memory(
        self,
        state: OrchestratorState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        result = await asyncio.to_thread(
            self.legacy.extract_candidate_memories_result,
            state["user_id"],
            state["session_id"],
            state["scene"],
            excluded_contents=state.get("excluded_memories", []),
        )
        payload = result.as_dict()
        if hasattr(queue, "timing_metrics"):
            payload["timing_ms"] = queue.timing_metrics()
        await queue.emit(
            "memory_extraction",
            payload,
            step="memory",
        )
        return {"memory_extraction": payload, "status": "memory_extracted"}

    def __getattr__(self, name: str):
        # UI 的记忆提取、保存和压缩接口继续委托给领域服务。
        return getattr(self.__dict__["legacy"], name)

    async def process_streaming(
        self,
        user_message: str,
        user_id: str,
        session_id: str,
        scene: str = "general",
        excluded_memories: Optional[List[str]] = None,
    ) -> AsyncGenerator[str, None]:
        queue = EventQueue()

        async def _run() -> None:
            try:
                thread_id = build_thread_id(user_id, scene, session_id)
                with timed_span(
                    "graph.turn",
                    trace_id=queue.trace_id,
                    attributes={"thread_id": thread_id, "scene": scene},
                ):
                    await self.graph.ainvoke(
                        {
                            "user_message": user_message,
                            "user_id": user_id,
                            "session_id": session_id,
                            "scene": scene,
                            "thread_id": thread_id,
                            "excluded_memories": list(excluded_memories or []),
                            "status": "started",
                        },
                        config={
                            "configurable": {
                                "thread_id": thread_id,
                                "event_queue": queue,
                            }
                        },
                    )
            except Exception as exc:
                message = f"处理失败: {type(exc).__name__}: {exc or '(无错误信息)'}"
                await emit_execution_log(
                    queue,
                    graph="langgraph",
                    node="process_streaming",
                    kind="graph_failed",
                    status="error",
                    step="graph",
                    details={"error_type": type(exc).__name__, "error": str(exc)},
                    message=message,
                )
                await queue.emit_error(message, step="graph")
            finally:
                await queue.finish()

        task = asyncio.create_task(_run())
        try:
            async for sse in queue.to_sse():
                yield sse
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


_langgraph_orchestrator = None


def get_langgraph_orchestrator(model_name: str = None) -> LangGraphOrchestrator:
    global _langgraph_orchestrator
    if _langgraph_orchestrator is None:
        _langgraph_orchestrator = LangGraphOrchestrator(OrchestratorV2(model_name))
    return _langgraph_orchestrator
