"""购物子图：检索规划 → 检索 → 生成 → 校验（失败一次修复）。

纯 LangGraph 版本：没有 legacy 编排对象，没有 config 注入队列。
服务从 ``runtime.context.services`` 取，事件走 ``runtime.stream_writer``。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from app.execution_log import emit_execution_log
from app.graphs.context import TurnContext
from app.graphs.state import ShoppingState
from app.reliability import RetryableError

# 节点级策略：LangGraph 原生 retry_policy / timeout，不再自研包装器。
PLAN_RETRY = RetryPolicy(
    max_attempts=2,
    initial_interval=0.05,
    retry_on=(RetryableError, ConnectionError, TimeoutError),
)


def _classification(state: ShoppingState):
    from app.classifier import ClassificationResult

    return ClassificationResult.model_validate(state.get("classification", {}))


async def plan_retrieval(state: ShoppingState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    ctx = runtime.context
    sink = ctx.sink_for(runtime)
    needs, strategy = await ctx.services.plan_shopping_retrieval(
        state["user_message"],
        _classification(state),
        sink,
    )
    await emit_execution_log(
        sink,
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


async def retrieve(state: ShoppingState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    ctx = runtime.context
    sink = ctx.sink_for(runtime)
    retrieval = await ctx.services.retrieve_shopping_context(
        state["user_message"],
        state["planned_strategy"],
        state["needs"],
        sink,
        user_id=state.get("user_id"),
    )
    source_status = retrieval.get("source_status", {}) if isinstance(retrieval, dict) else {}
    failed_sources = [
        name for name, status in source_status.items() if status not in {"ok", "not_selected"}
    ]
    failure_details = []
    for source in failed_sources:
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
        sink,
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
        message=(f"检索结束；{'; '.join(failure_details)}" if failed_sources else "商品与评价检索完成"),
    )
    return {"retrieval": retrieval, "status": "retrieved"}


async def generate(state: ShoppingState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    ctx = runtime.context
    # 商品/预算卡片只依赖检索结果，生成前就能定稿：先发出去，
    # 前端在正文写之前就把卡片画好，结束时不再整块换版式。
    try:
        from app.ui.blocks import shopping_blocks

        blocks = shopping_blocks(
            state.get("retrieval") or {},
            "",
            query=state["user_message"],
        )
        if blocks:
            await ctx.sink_for(runtime).emit("answer_blocks", {
                "agent": "shopping",
                "blocks": blocks,
            }, step="generate")
    except Exception:
        pass
    response = await ctx.services.generate_shopping_response(
        state["user_message"],
        state["user_id"],
        state["thread_id"],
        _classification(state),
        state["retrieval"],
        ctx.sink_for(runtime),
    )
    return {"response": response, "status": "generated"}


async def validate(state: ShoppingState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """一次校验；不通过则执行唯一一次修复后再次校验。"""
    ctx = runtime.context
    sink = ctx.sink_for(runtime)
    services = ctx.services
    response = state.get("response", "")
    try:
        services.validate_shopping_response(response, state.get("retrieval", {}))
    except (TypeError, ValueError) as exc:
        await emit_execution_log(
            sink,
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
        await sink.emit_thinking(f"购物回答校验未通过，正在一次修复：{exc}", step="generate")
        await sink.emit_response_reset(step="generate")
        response = await services.generate_shopping_response(
            state["user_message"],
            state["user_id"],
            state["thread_id"],
            _classification(state),
            state["retrieval"],
            sink,
            validation_feedback=str(exc),
            constraints=state.get("constraints"),
        )
        services.validate_shopping_response(response, state.get("retrieval", {}))
        await emit_execution_log(
            sink,
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
            sink,
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


def build_shopping_graph(checkpointer: Optional[Any] = None):
    graph = StateGraph(ShoppingState, context_schema=TurnContext)
    graph.add_node("plan_retrieval", plan_retrieval, retry_policy=PLAN_RETRY, timeout=10)
    graph.add_node("retrieve", retrieve, timeout=30)
    graph.add_node("generate", generate, timeout=120)
    # validate 内含一次完整模型修复，超时与 generate 同级。
    graph.add_node("validate", validate, timeout=120)
    graph.add_edge(START, "plan_retrieval")
    graph.add_edge("plan_retrieval", "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "validate")
    graph.add_edge("validate", END)
    return graph.compile(checkpointer=checkpointer)
