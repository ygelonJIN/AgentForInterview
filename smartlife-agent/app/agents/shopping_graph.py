"""购物子图：检索规划、检索、生成和回答校验。"""
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph


class ShoppingState(TypedDict, total=False):
    user_message: str
    user_id: str
    thread_id: str
    classification: Dict[str, Any]
    needs: Dict[str, Any]
    planned_strategy: str
    retrieval: Dict[str, Any]
    response: str
    status: str


class ShoppingGraph:
    """把购物控制流显式化，检索和生成仍由 OrchestratorV2 领域方法执行。"""

    def __init__(self, legacy: Any):
        self.legacy = legacy
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(ShoppingState)
        graph.add_node("plan_retrieval", self._plan_retrieval)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("generate", self._generate)
        graph.add_node("validate", self._validate)
        graph.add_edge(START, "plan_retrieval")
        graph.add_edge("plan_retrieval", "retrieve")
        graph.add_edge("retrieve", "generate")
        graph.add_edge("generate", "validate")
        graph.add_edge("validate", END)
        return graph.compile()

    @staticmethod
    def _queue_from(config: Optional[RunnableConfig]):
        queue = (config or {}).get("configurable", {}).get("event_queue")
        if queue is None:
            raise RuntimeError("缺少运行期 EventQueue")
        return queue

    @staticmethod
    def _classification(state: ShoppingState):
        from app.classifier import ClassificationResult
        return ClassificationResult.model_validate(state.get("classification", {}))

    async def _plan_retrieval(
        self,
        state: ShoppingState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        needs, strategy = await self.legacy.plan_shopping_retrieval(
            state["user_message"],
            self._classification(state),
            queue,
        )
        return {"needs": needs, "planned_strategy": strategy, "status": "planned"}

    async def _retrieve(
        self,
        state: ShoppingState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        retrieval = await self.legacy.retrieve_shopping_context(
            state["user_message"],
            state["planned_strategy"],
            state["needs"],
            queue,
        )
        return {"retrieval": retrieval, "status": "retrieved"}

    async def _generate(
        self,
        state: ShoppingState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        response = await self.legacy.generate_shopping_response(
            state["user_message"],
            state["user_id"],
            state["thread_id"],
            self._classification(state),
            state["retrieval"],
            queue,
        )
        return {"response": response, "status": "generated"}

    async def _validate(self, state: ShoppingState) -> Dict[str, Any]:
        self.legacy.validate_shopping_response(state.get("response", ""), state.get("retrieval", {}))
        return {"status": "validated"}

    async def run_streaming(
        self,
        user_message: str,
        user_id: str,
        thread_id: str,
        classification: Dict[str, Any],
        queue: Any,
    ) -> str:
        result = await self.graph.ainvoke(
            {
                "user_message": user_message,
                "user_id": user_id,
                "thread_id": thread_id,
                "classification": classification,
                "status": "started",
            },
            config={"configurable": {"thread_id": thread_id, "event_queue": queue}},
        )
        return result.get("response", "")
