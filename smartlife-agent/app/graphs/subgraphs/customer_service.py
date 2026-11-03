"""订单客服子图：只读当前 Actor 的订单，再基于真实数据生成回答。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.graphs.context import TurnContext
from app.graphs.state import CustomerServiceState


async def load_orders(state: CustomerServiceState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    actor_id = state.get("user_id", "")
    if not actor_id:
        raise PermissionError("客服订单查询缺少 Actor")
    orders = await asyncio.to_thread(
        runtime.context.services.order_repository.get_order_status,
        actor_id,
        None,
    )
    sink = runtime.context.sink_for(runtime)
    await sink.emit_tool_result(
        "order_repository",
        f"返回 {len(orders or [])} 条订单",
        step="retrieval",
    )
    return {"orders": list(orders or []), "status": "orders_loaded"}


async def generate(state: CustomerServiceState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    orders = state.get("orders") or []
    order_text = "\n".join(
        f"- id={item.get('id')} product={item.get('product_name')} "
        f"status={item.get('status')} total={item.get('total_price')}"
        for item in orders[:20]
    ) or "当前用户暂无订单记录。"
    prompt = ChatPromptTemplate.from_messages([
        ("system", """你是订单客服助手。只根据下方真实订单数据回答。
不得编造订单、物流、退款状态。没有数据时明确说没有查到。
退换货、退款、投诉只能说明当前项目只读，不执行支付或退款操作。"""),
        ("user", "用户问题：{question}\n\n真实订单：\n{orders}"),
    ])
    chain = prompt | runtime.context.services.llm
    full = ""
    async for chunk in chain.astream({"question": state["user_message"], "orders": order_text}):
        if chunk.content:
            full += str(chunk.content)
            await sink.emit_token(str(chunk.content), step="generate")
    return {"response": full, "status": "generated"}


def build_customer_service_graph(checkpointer: Optional[Any] = None):
    graph = StateGraph(CustomerServiceState, context_schema=TurnContext)
    graph.add_node("load_orders", load_orders, timeout=5)
    graph.add_node("generate", generate, timeout=60)
    graph.add_edge(START, "load_orders")
    graph.add_edge("load_orders", "generate")
    graph.add_edge("generate", END)
    return graph.compile(checkpointer=checkpointer)
