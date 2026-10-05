"""通用敏感操作审批图。"""
import asyncio
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class ActionApprovalState(TypedDict, total=False):
    approval_id: str
    user_id: str
    action_type: str
    payload: Dict[str, Any]
    decision: Dict[str, Any]
    result: Any
    status: str


class ActionApprovalGraph:
    """用 interrupt 暂停敏感操作，用户批准后在同一 thread 恢复执行。"""

    def __init__(
        self,
        execute: Callable[..., Any],
        *,
        action_type: str = "action",
        approved_actions: Iterable[str] = ("approve",),
        checkpointer=None,
        interrupt_payload: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        if not action_type:
            raise ValueError("action_type 不能为空")
        self.execute = execute
        self.action_type = action_type
        self.approved_actions = set(approved_actions or ())
        if not self.approved_actions:
            raise ValueError("approved_actions 不能为空")
        self.checkpointer = checkpointer or InMemorySaver()
        self.interrupt_payload = interrupt_payload
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(ActionApprovalState)
        graph.add_node("request_approval", self._request_approval)
        graph.add_node("execute", self._execute)
        graph.add_node("reject", self._reject)
        graph.add_edge(START, "request_approval")
        graph.add_conditional_edges(
            "request_approval",
            self._route_decision,
            {"execute": "execute", "reject": "reject"},
        )
        graph.add_edge("execute", END)
        graph.add_edge("reject", END)
        return graph.compile(checkpointer=self.checkpointer)

    def _request_approval(self, state: ActionApprovalState) -> Dict[str, Any]:
        payload = {
            "approval_type": state.get("action_type", self.action_type),
            "approval_id": state["approval_id"],
            "user_id": state["user_id"],
            "payload": state.get("payload", {}),
            "actions": sorted(self.approved_actions | {"reject"}),
        }
        if self.interrupt_payload:
            payload = self.interrupt_payload(payload)
        decision = interrupt(payload)
        return {"decision": decision if isinstance(decision, dict) else {"action": "reject"}}

    def _route_decision(self, state: ActionApprovalState) -> str:
        action = state.get("decision", {}).get("action")
        return "execute" if action in self.approved_actions else "reject"

    async def _execute(self, state: ActionApprovalState) -> Dict[str, Any]:
        result = self.execute(
            state.get("user_id", ""),
            state.get("action_type", self.action_type),
            state.get("payload", {}),
            state.get("decision", {}),
        )
        if hasattr(result, "__await__"):
            result = await result
        return {"result": result, "status": "executed"}

    @staticmethod
    async def _reject(_state: ActionApprovalState) -> Dict[str, Any]:
        return {"result": None, "status": "rejected"}

    @staticmethod
    def _clean_result(result: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in result.items() if key != "__interrupt__"}

    def start(
        self,
        approval_id: str,
        user_id: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        action_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not approval_id:
            raise ValueError("approval_id 不能为空")
        if not user_id:
            raise ValueError("user_id 不能为空")
        resolved_type = action_type or self.action_type
        if resolved_type != self.action_type:
            raise ValueError("action_type 与审批图不匹配")
        result = self.graph.invoke(
            {
                "approval_id": approval_id,
                "user_id": user_id,
                "action_type": resolved_type,
                "payload": dict(payload or {}),
                "decision": {},
                "result": None,
                "status": "awaiting_approval",
            },
            config={"configurable": {"thread_id": approval_id}},
        )
        interrupts = result.get("__interrupt__", [])
        return {
            "approval_id": approval_id,
            "status": "awaiting_approval",
            "interrupt": interrupts[0].value if interrupts else None,
            "state": self._clean_result(result),
        }

    async def aresume(self, approval_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
        if not approval_id:
            raise ValueError("approval_id 不能为空")
        result = await self.graph.ainvoke(
            Command(resume=decision if isinstance(decision, dict) else {"action": "reject"}),
            config={"configurable": {"thread_id": approval_id}},
        )
        return {
            "approval_id": approval_id,
            "status": result.get("status", "completed"),
            "state": self._clean_result(result),
        }

    def resume(self, approval_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
        return asyncio.run(self.aresume(approval_id, decision))
