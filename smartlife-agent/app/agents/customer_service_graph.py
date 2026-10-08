"""客服订单查询子图：固定 Repository 数据源 + 生成 + 权限校验。"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph

from app.execution_log import CallbackEventQueue, build_execution_log, run_policy_node
from app.reliability import NodePolicy, RetryPolicy


class CustomerServiceState(TypedDict, total=False):
    user_message: str
    actor_id: str
    history: List[Any]
    orders: List[Dict[str, Any]]
    response: str
    status: str


class CustomerServiceGraph:
    """只允许通过 OrderRepository 访问当前 Actor 的订单。"""

    def __init__(self, order_repository, llm, *, node_policies=None):
        self.order_repository = order_repository
        self.llm = llm
        self.node_policies = node_policies or {
            "load_orders": NodePolicy(timeout_seconds=5),
            "generate": NodePolicy(timeout_seconds=60),
        }
        self.graph = self._build_graph()

    def _policy_node(self, name: str, node):
        policy = self.node_policies[name]

        async def run(state: CustomerServiceState):
            async def operation(_attempt: int):
                return await node(state)
            return await policy.run(operation, on_event=self._on_retry_event)
        return run

    async def _on_retry_event(self, event: str, data: Dict[str, Any]) -> None:
        return None

    def _build_graph(self):
        graph = StateGraph(CustomerServiceState)
        graph.add_node("load_orders", self._policy_node("load_orders", self._load_orders))
        graph.add_node("generate", self._policy_node("generate", self._generate))
        graph.add_edge(START, "load_orders")
        graph.add_edge("load_orders", "generate")
        graph.add_edge("generate", END)
        return graph.compile()

    async def _load_orders(self, state: CustomerServiceState) -> Dict[str, Any]:
        actor_id = state["actor_id"]
        if not actor_id:
            raise PermissionError("客服订单查询缺少 Actor")
        orders = await asyncio.to_thread(
            self.order_repository.get_order_status,
            actor_id,
            None,
        )
        return {"orders": list(orders or []), "status": "orders_loaded"}

    async def _generate(self, state: CustomerServiceState) -> Dict[str, Any]:
        orders = state.get("orders") or []
        order_text = "\n".join(
            f"- id={item.get('id')} product={item.get('product_name')} status={item.get('status')} total={item.get('total_price')}"
            for item in orders[:20]
        ) or "当前用户暂无订单记录。"
        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是订单客服助手。只根据下方真实订单数据回答。
不得编造订单、物流、退款状态。没有数据时明确说没有查到。
退换货、退款、投诉只能说明当前项目只读，不执行支付或退款操作。"""),
            ("user", "用户问题：{question}\n\n真实订单：\n{orders}"),
        ])
        chain = prompt | self.llm
        full = ""
        async for chunk in chain.astream({"question": state["user_message"], "orders": order_text}):
            if chunk.content:
                full += str(chunk.content)
        return {"response": full, "status": "generated"}

    async def run_streaming(self, user_message: str, actor_id: str, queue=None, history=None) -> str:
        result = await self.graph.ainvoke({
            "user_message": user_message,
            "actor_id": actor_id,
            "history": list(history or []),
            "status": "started",
        })
        if queue:
            await queue.emit_tool_result("order_repository", f"返回 {len(result.get('orders') or [])} 条订单", step="retrieval")
            if result.get("response"):
                await queue.emit_token(result["response"], step="generate")
        return result.get("response", "")
