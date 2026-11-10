"""旅行子图：问答走 chat，规划走 trip_gear 的无装备版本。

travel 不是第二条链路：它就是 ``trip_gear`` 剥掉装备段（gear / owned / shop）
之后的同一张图、同一套面板。两个节点共用一套实现，差别只在 ``with_gear``。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.graphs.context import TurnContext
from app.graphs.subgraphs.trip_gear import build_trip_gear_graph


class TravelState(TypedDict, total=False):
    # 输入（父图透传）
    user_message: str
    user_id: str
    thread_id: str
    classification: Dict[str, Any]
    constraints: Dict[str, Any]
    chat_history: List[Any]

    # 输出
    response: str
    status: str


async def travel_chat(state: TravelState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """单点问答（"杭州适合几月去"）：不进规划链路，直接流式回答。"""
    ctx = runtime.context
    response = await ctx.services.travel_service.chat_streaming(
        state.get("user_message", ""),
        state.get("user_id", ""),
        ctx.sink_for(runtime),
    )
    try:
        from app.ui.blocks import text_only

        await ctx.sink_for(runtime).emit("answer_blocks", {
            "agent": "travel",
            "blocks": text_only(response),
        }, step="generate")
    except Exception:
        pass
    return {"response": response, "status": "generated"}


def _needs_plan(state: TravelState) -> str:
    route = str((state.get("classification") or {}).get("route") or "react")
    return "travel_plan" if route == "plan_and_execute" else "travel_chat"


def build_travel_graph(checkpointer: Optional[Any] = None):
    """按 route 分流：单点问答走 chat，多天规划走无装备版 trip_gear。"""
    plan_graph = build_trip_gear_graph(with_gear=False)
    graph = StateGraph(TravelState, context_schema=TurnContext)
    graph.add_node("travel_plan", plan_graph)
    graph.add_node("travel_chat", travel_chat, timeout=120)
    graph.add_conditional_edges(
        START,
        _needs_plan,
        {"travel_plan": "travel_plan", "travel_chat": "travel_chat"},
    )
    graph.add_edge("travel_plan", END)
    graph.add_edge("travel_chat", END)
    return graph.compile(checkpointer=checkpointer)
