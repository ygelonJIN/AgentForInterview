"""
LangGraph 顶层编排器。

顶层图负责分类、条件路由、场景执行和最终状态提交。
领域实现继续由 OrchestratorV2 的服务方法承担，避免重写 SQL/RAG/模型/工具。
"""
import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agents.orchestrator_v2 import OrchestratorV2
from app.checkpointing import create_checkpointer
from app.classifier import ClassificationResult, UnifiedClassifier
from app.session import build_thread_id
from app.streaming import EventQueue


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
    ):
        self.legacy = legacy_orchestrator or OrchestratorV2()
        self.checkpointer = checkpointer or create_checkpointer("memory")
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(OrchestratorState)
        graph.add_node("prepare", self._prepare)
        graph.add_node("classify", self._classify)
        graph.add_node("shopping", self._shopping)
        graph.add_node("travel", self._travel)
        graph.add_node("negotiation", self._negotiation)
        graph.add_node("react", self._react)
        graph.add_node("general", self._general)
        graph.add_node("finalize", self._finalize)
        graph.add_node("extract_memory", self._extract_memory)

        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "classify")
        graph.add_conditional_edges(
            "classify",
            self._route,
            {
                "shopping": "shopping",
                "travel": "travel",
                "negotiation": "negotiation",
                "react": "react",
                "general": "general",
            },
        )
        for scene_node in ("shopping", "travel", "negotiation", "react", "general"):
            graph.add_edge(scene_node, "finalize")
        graph.add_edge("finalize", "extract_memory")
        graph.add_edge("extract_memory", END)
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
            "agent_used": classification.intent,
            "status": "classified",
        }

    @staticmethod
    def _route(state: OrchestratorState) -> str:
        classification = state.get("classification", {})
        intent = classification.get("intent", "general")
        route = classification.get("route", "react")
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
        return {"response": response, "agent_used": classification.intent, "status": "generated"}

    async def _shopping(self, state: OrchestratorState, config: Optional[RunnableConfig] = None):
        return await self._run_scene(state, config, "shopping")

    async def _travel(self, state: OrchestratorState, config: Optional[RunnableConfig] = None):
        return await self._run_scene(state, config, "travel")

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
        return {"status": "completed"}

    async def _extract_memory(
        self,
        state: OrchestratorState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        result = self.legacy.extract_candidate_memories_result(
            state["user_id"],
            state["session_id"],
            state["scene"],
            excluded_contents=state.get("excluded_memories", []),
        )
        payload = result.as_dict()
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
                await queue.emit_error(f"处理失败: {exc}")
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
