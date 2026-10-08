"""购物子图：检索规划、检索、生成和回答校验。"""
import inspect
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.execution_log import emit_execution_log, run_policy_node
from app.reliability import NodePolicy, RetryPolicy, RetryableError


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

    def __init__(self, legacy: Any, node_policies: Optional[Dict[str, NodePolicy]] = None):
        self.legacy = legacy
        self.node_policies = node_policies or {
            "plan_retrieval": NodePolicy(
                timeout_seconds=10,
                retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.05),
                retryable_exceptions=(RetryableError, ConnectionError, TimeoutError),
            ),
            "retrieve": NodePolicy(timeout_seconds=30, retry_policy=RetryPolicy(max_attempts=1)),
            "generate": NodePolicy(timeout_seconds=120, retry_policy=RetryPolicy(max_attempts=1)),
            # validate 在校验失败时会执行一次完整模型修复，因此必须与 generate 同级。
            "validate": NodePolicy(timeout_seconds=120, retry_policy=RetryPolicy(max_attempts=1)),
        }
        self.graph = self._build_graph()

    def _policy_node(self, name: str, node):
        policy = self.node_policies[name]
        accepts_config = "config" in inspect.signature(node).parameters

        async def run(state: ShoppingState, config: Optional[RunnableConfig] = None):
            queue = self._queue_from(config)

            async def operation(_attempt: int):
                return await node(state, config) if accepts_config else await node(state)

            return await run_policy_node(
                policy,
                operation,
                queue=queue,
                graph="shopping",
                node=name,
                step={
                    "plan_retrieval": "retrieval",
                    "retrieve": "retrieval",
                    "generate": "generate",
                    "validate": "generate",
                }.get(name, name),
                details={
                    "strategy": state.get("planned_strategy", ""),
                    "thread_id": state.get("thread_id", ""),
                },
            )

        return run

    def _build_graph(self):
        graph = StateGraph(ShoppingState)
        graph.add_node("plan_retrieval", self._policy_node("plan_retrieval", self._plan_retrieval))
        graph.add_node("retrieve", self._policy_node("retrieve", self._retrieve))
        graph.add_node("generate", self._policy_node("generate", self._generate))
        graph.add_node("validate", self._policy_node("validate", self._validate))
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
        await emit_execution_log(
            queue,
            graph="shopping",
            node="plan_retrieval",
            kind="branch_selected",
            status="ok",
            step="retrieval",
            branch=strategy,
            details={"needs": needs},
            message=(
                f"检索计划选择 {strategy}："
                f"商品 {'是' if needs.get('needs_product') else '否'}，"
                f"评价 {'是' if needs.get('needs_review') else '否'}"
            ),
        )
        return {"needs": needs, "planned_strategy": strategy, "status": "planned"}

    async def _retrieve(
        self,
        state: ShoppingState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        try:
            retrieval = await self.legacy.retrieve_shopping_context(
                state["user_message"],
                state["planned_strategy"],
                state["needs"],
                queue,
                user_id=state.get("user_id"),
            )
        except TypeError as exc:
            # 兼容旧的 legacy 测试/扩展实现；仅在明确不接受 user_id 时降级。
            if "user_id" not in str(exc):
                raise
            retrieval = await self.legacy.retrieve_shopping_context(
                state["user_message"],
                state["planned_strategy"],
                state["needs"],
                queue,
            )
        source_status = retrieval.get("source_status", {}) if isinstance(retrieval, dict) else {}
        failed_sources = [
            name for name, status in source_status.items()
            if status not in {"ok", "not_selected"}
        ]
        failure_details = []
        for source in failed_sources:
            stage = ""
            if source == "sql":
                stage = {
                    "nl2sql_model": "小模型生成 SQL 失败",
                    "sql_execution": "SQLite 执行 SQL 失败",
                    "empty_result": "SQL 查询无商品结果",
                }.get(str(retrieval.get("sql_failure_stage") or ""), "SQL 数据源失败")
            else:
                stage = f"{source.upper()} 数据源失败"
            error = str(retrieval.get("source_errors", {}).get(source, "")).strip()
            failure_details.append(f"{source}: {stage}" + (f"，{error[:120]}" if error else ""))
        await emit_execution_log(
            queue,
            graph="shopping",
            node="retrieve",
            kind="degradation" if failed_sources else "retrieval_completed",
            status="warning" if failed_sources else "ok",
            step="retrieval",
            details={
                "source_status": source_status,
                "source_errors": retrieval.get("source_errors", {}),
                "sql_failure_stage": retrieval.get("sql_failure_stage", ""),
                "source_timings_ms": retrieval.get("source_timings_ms", {}),
                "source_timeout_seconds": retrieval.get("source_timeout_seconds"),
                "partial": retrieval.get("partial", False),
            },
            message=(
                f"检索结束；{'; '.join(failure_details)}"
                if failed_sources else "商品与评价检索完成"
            ),
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

    async def _validate(
        self,
        state: ShoppingState,
        config: Optional[RunnableConfig] = None,
    ) -> Dict[str, Any]:
        queue = self._queue_from(config)
        response = state.get("response", "")
        try:
            self.legacy.validate_shopping_response(response, state.get("retrieval", {}))
        except (TypeError, ValueError) as exc:
            await emit_execution_log(
                queue,
                graph="shopping",
                node="validate",
                kind="validation_failed",
                status="warning",
                step="generate",
                iteration=1,
                max_iterations=1,
                details={"error_type": type(exc).__name__, "error": str(exc)},
                message=f"回答校验失败，进入唯一一次修复：{exc}",
            )
            if hasattr(queue, "emit_thinking"):
                await queue.emit_thinking(
                    f"购物回答校验未通过，正在一次修复：{exc}",
                    step="generate",
                )
            response = await self.legacy.generate_shopping_response(
                state["user_message"],
                state["user_id"],
                state["thread_id"],
                self._classification(state),
                state["retrieval"],
                queue,
                validation_feedback=str(exc),
            )
            self.legacy.validate_shopping_response(response, state.get("retrieval", {}))
            await emit_execution_log(
                queue,
                graph="shopping",
                node="validate",
                kind="repair_succeeded",
                status="ok",
                step="generate",
                iteration=1,
                max_iterations=1,
                message="一次修复后校验通过",
            )
        else:
            await emit_execution_log(
                queue,
                graph="shopping",
                node="validate",
                kind="validation_succeeded",
                status="ok",
                step="generate",
                iteration=0,
                max_iterations=1,
                message="回答一次校验通过",
            )
        return {"response": response, "status": "validated"}

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
