"""以自然语言调用单个 Agent，返回结构化结果。

MCP 的 ``run`` 工具就是它的薄封装：外部系统不必自己拼天气 + 商品，
直接把需求交给某个 Agent。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


async def run_agent(
    agent_id: str,
    request: str,
    *,
    user_id: str,
    session_id: str = "mcp",
    services: Optional[Any] = None,
) -> Dict[str, Any]:
    """点名一个 Agent 跑一轮，返回正文、分类与结构化方案（如果有）。"""
    from app.agents.specs import get_spec
    from app.graphs.orchestrator import TurnGraph

    spec = get_spec(agent_id)
    graph = TurnGraph(services=services)
    state = await graph.invoke(
        str(request),
        str(user_id),
        session_id,
        scene=spec.agent_id,
        agent=spec.agent_id,
    )
    payload: Dict[str, Any] = {
        "agent": spec.agent_id,
        "user_id": user_id,
        "response": state.get("response") or "",
        "status": state.get("status") or "",
    }
    classification = state.get("classification")
    if isinstance(classification, dict):
        payload["classification"] = {
            "intent": classification.get("intent"),
            "route": classification.get("route"),
            "confidence": classification.get("confidence"),
        }
    # trip_gear / travel 的结构化结果：约束、行程、装备、预算、结论
    for key in ("facts", "constraints", "itinerary", "gear", "products", "issues", "verdict"):
        if state.get(key):
            payload[key] = state[key]
    return payload
