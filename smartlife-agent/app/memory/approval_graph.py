"""
候选记忆审批图。

保留旧的 MemoryApprovalGraph API，内部复用通用 ActionApprovalGraph。
"""
from typing import Any, Callable, Dict, List, Optional

from app.approval_graph import ActionApprovalGraph
from app.checkpointing import create_checkpointer


class MemoryApprovalGraph:
    """候选记忆从提取到人工确认再到持久化的适配器。"""

    def __init__(self, persist: Callable[..., Any], checkpointer=None):
        self.persist = persist
        self._approval = ActionApprovalGraph(
            self._execute_memory,
            action_type="memory",
            approved_actions=("save_selected", "save_all"),
            checkpointer=checkpointer or create_checkpointer(),
            interrupt_payload=self._interrupt_payload,
        )
        self.checkpointer = self._approval.checkpointer
        self.graph = self._approval.graph

    @staticmethod
    def _interrupt_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        candidates = payload.get("payload", {}).get("candidates", [])
        return {
            "approval_type": "memory",
            "approval_id": payload["approval_id"],
            "user_id": payload["user_id"],
            "candidates": candidates,
            "actions": ["save_selected", "save_all", "reject"],
        }

    def _execute_memory(
        self,
        user_id: str,
        _action_type: str,
        payload: Dict[str, Any],
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        candidates = list(payload.get("candidates", []))
        if decision.get("action") == "save_all":
            selected_indices = list(range(len(candidates)))
        else:
            selected_indices = [
                int(index) for index in decision.get("selected_indices", [])
                if isinstance(index, (int, float, str)) and str(index).isdigit()
            ]
        saved_ids = self.persist(user_id, candidates, selected_indices)
        return {
            "selected_indices": selected_indices,
            "saved_ids": list(saved_ids or []),
        }

    @staticmethod
    def _map_state(state: Dict[str, Any], candidates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        result = state.pop("result", None) or {}
        mapped = {
            "approval_id": state.get("approval_id", ""),
            "user_id": state.get("user_id", ""),
            "candidates": list(candidates if candidates is not None else state.get("payload", {}).get("candidates", [])),
            "decision": state.get("decision", {}),
            "selected_indices": list(result.get("selected_indices", [])),
            "saved_ids": list(result.get("saved_ids", [])),
            "status": state.get("status", "completed"),
        }
        return mapped

    def start(
        self,
        approval_id: str,
        user_id: str,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not approval_id:
            raise ValueError("approval_id 不能为空")
        result = self._approval.start(
            approval_id,
            user_id,
            {"candidates": list(candidates or [])},
            action_type="memory",
        )
        state = self._map_state(result["state"], candidates)
        state["status"] = "awaiting_approval"
        return {
            "approval_id": approval_id,
            "status": "awaiting_approval",
            "interrupt": result["interrupt"],
            "state": state,
        }

    def resume(self, approval_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
        if not approval_id:
            raise ValueError("approval_id 不能为空")
        result = self._approval.resume(
            approval_id,
            decision if isinstance(decision, dict) else {"action": "reject"},
        )
        mapped_state = self._map_state(result["state"])
        status = "saved" if result.get("status") == "executed" else result.get("status", "completed")
        mapped_state["status"] = status
        return {
            "approval_id": approval_id,
            "status": status,
            "state": mapped_state,
        }
