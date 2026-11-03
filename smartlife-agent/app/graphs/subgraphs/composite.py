"""复合意图（购物 + 旅行）子图：并行分支 + 归约合并。

旧实现是在一个节点里 ``asyncio.gather`` 两个 legacy 方法；
现在用 LangGraph 的并行边 + ``branch_results`` reducer 表达同一语义，
分支失败通过 BranchOutcome 显式记录，不再依赖异常穿透。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.services.task_models import SharedConstraints, branch_outcome
from app.classifier import ClassificationResult
from app.graphs.context import TurnContext
from app.graphs.state import TurnState
from app.graphs.subgraphs.shopping import build_shopping_graph
from app.graphs.subgraphs.travel import build_travel_graph


def _branch_state(state: TurnState, intent: str) -> Dict[str, Any]:
    classification = ClassificationResult.model_validate(state.get("classification") or {})
    if intent == "shopping":
        branch_classification = classification.model_copy(update={
            "intent": "shopping",
            "intents": ["shopping"],
            "sub_intent": "search",
            "retrieval": "mixed",
            "intent_scores": {"shopping": classification.intent_scores.get("shopping", 1.0)},
        })
    else:
        branch_classification = classification.model_copy(update={
            "intent": "travel",
            "intents": ["travel"],
            "route": "plan_and_execute",
            "sub_intent": "plan_trip",
            "retrieval": "rag_only",
            "intent_scores": {"travel": classification.intent_scores.get("travel", 1.0)},
        })
    return {
        "user_message": state["user_message"],
        "user_id": state["user_id"],
        "thread_id": state.get("thread_id", ""),
        "classification": branch_classification.model_dump(),
        "constraints": dict(state.get("constraints") or {}),
        "status": "started",
    }


async def _run_branch(state: TurnState, runtime: Runtime[TurnContext], intent: str, graph) -> Dict[str, Any]:
    ctx = runtime.context
    writer = get_stream_writer()
    branch_ctx = ctx.with_extra(branch=intent).bind_writer(writer)
    try:
        result = await graph.ainvoke(_branch_state(state, intent), context=branch_ctx)
        text = str(result.get("response") or "").strip()
        outcome = branch_outcome(intent, text)
    except Exception as exc:  # 单分支失败不拖垮另一个分支
        outcome = branch_outcome(intent, error=f"{type(exc).__name__}: {exc}")
    return {"branch_results": [outcome.model_dump()]}


async def shopping_branch(state: TurnState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    return await _run_branch(state, runtime, "shopping", build_shopping_graph())


async def travel_branch(state: TurnState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    return await _run_branch(state, runtime, "travel", build_travel_graph())


async def synthesize(state: TurnState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    ctx = runtime.context
    outcomes = {item.get("branch"): item for item in state.get("branch_results") or []}
    constraints = SharedConstraints.model_validate(state.get("constraints") or {})
    response = await ctx.services.synthesize_composite_response(
        state["user_message"],
        outcomes.get("shopping", {"status": "failed", "error": "购物分支缺失"}),
        outcomes.get("travel", {"status": "failed", "error": "旅行分支缺失"}),
        constraints,
        ctx.sink_for(runtime),
    )
    ok = any(item.get("status") == "ok" for item in outcomes.values())
    return {"response": response, "agent_used": "shopping+travel", "status": "generated" if ok else "failed"}


def build_composite_graph(checkpointer: Optional[Any] = None):
    graph = StateGraph(TurnState, context_schema=TurnContext)
    graph.add_node("shopping_branch", shopping_branch, timeout=300)
    graph.add_node("travel_branch", travel_branch, timeout=300)
    graph.add_node("synthesize", synthesize, timeout=120)
    graph.add_edge(START, "shopping_branch")
    graph.add_edge(START, "travel_branch")
    graph.add_edge("shopping_branch", "synthesize")
    graph.add_edge("travel_branch", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile(checkpointer=checkpointer)
