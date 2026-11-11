"""旅行子图：行程图去掉装备段，只有这一条路。

``travel`` 不是第二条链路：它就是 ``build_trip_gear_graph(with_gear=False)``，
与 trip_gear 共用 ``derive → plan → verify → repair → finalize``，差别只有一个参数——
不推导装备、不检索商品、不输出装备分区。

早先这里另有一条 ``travel_chat``：不进规划，直接把问题丢给大模型写正文。
结果是同一句话有时出 01–05 面板、有时出一整篇 Markdown，而且缺天数/人数/日期时
模型会自己替用户定（"8天7晚"）。现在删掉：所有 travel 请求都进这张图，
缺信息由 ``clarify`` 追问，结构化结果只有一处产生。
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


def build_travel_graph(checkpointer: Optional[Any] = None):
    """旅行 = 无装备版行程图；没有第二条"直接问大模型"的分支。

    保留这一层单节点包装而不是直接返回子图，是为了让
    ``scripts/build_pipeline_spec.py`` 的静态解析仍能从 AST 读出
    "travel 节点挂了哪张图"。
    """
    plan_graph = build_trip_gear_graph(with_gear=False)
    graph = StateGraph(TravelState, context_schema=TurnContext)
    graph.add_node("travel_plan", plan_graph)
    graph.add_edge(START, "travel_plan")
    graph.add_edge("travel_plan", END)
    return graph.compile(checkpointer=checkpointer)
