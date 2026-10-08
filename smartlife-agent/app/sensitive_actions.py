"""敏感副作用的统一审批执行服务。

当前项目没有支付或创建订单功能，服务只覆盖记忆管理副作用。
"""
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from app.approval_graph import ActionApprovalGraph
from app.checkpointing import create_checkpointer


class SensitiveActionService:
    """为记忆管理副作用提供统一的审批与幂等执行入口。"""

    def __init__(
        self,
        *,
        memory_repository=None,
    ):
        self.memory_repository = memory_repository
        self._graphs: Dict[str, ActionApprovalGraph] = {}
        self._approval_types: Dict[str, str] = {}
        self._receipts: Dict[str, Any] = {}

    def _handlers(self) -> Dict[str, Callable[..., Any]]:
        def memory_delete(_user_id, payload, _decision):
            if not self.memory_repository:
                raise RuntimeError("记忆仓储未初始化")
            return {"deleted": self.memory_repository.delete_memory(
                payload["user_id"], payload["memory_id"]
            )}

        def memory_update(_user_id, payload, _decision):
            if not self.memory_repository:
                raise RuntimeError("记忆仓储未初始化")
            return {"updated": self.memory_repository.update_memory(
                payload["user_id"], payload["memory_id"], payload["content"]
            )}

        def memory_clear(user_id, _payload, _decision):
            if not self.memory_repository:
                raise RuntimeError("记忆仓储未初始化")
            return {"deleted_count": self.memory_repository.clear_user_memories(user_id)}

        def memory_save_summary(_user_id, payload, _decision):
            if not self.memory_repository:
                raise RuntimeError("记忆仓储未初始化")
            metadata = {
                "source": "conversation_summary",
                "thread_id": payload.get("thread_id", ""),
                "conversation_hash": payload.get("conversation_hash", ""),
            }
            memory_id = self.memory_repository.save_summary(
                payload["user_id"], payload["summary"], metadata
            )
            if payload.get("thread_id") and payload.get("conversation_hash"):
                from app.memory.conversation_store import ConversationStore
                ConversationStore().register_summary(
                    payload["thread_id"],
                    payload["conversation_hash"],
                    payload["summary"],
                    memory_id=memory_id,
                )
            return {"memory_id": memory_id}

        def memory_sync_markdown(_user_id, payload, _decision):
            if not self.memory_repository:
                raise RuntimeError("记忆仓储未初始化")
            return self.memory_repository.replace_markdown_documents(
                payload["user_id"],
                payload.get("preferences_content", ""),
                payload.get("events_content", ""),
            )

        def memory_update_vector(_user_id, payload, _decision):
            if not self.memory_repository:
                raise RuntimeError("记忆仓储未初始化")
            return self.memory_repository.update_vector_record(
                payload["user_id"], payload["vector_id"], payload["content"]
            )

        def memory_delete_vector(_user_id, payload, _decision):
            if not self.memory_repository:
                raise RuntimeError("记忆仓储未初始化")
            return self.memory_repository.delete_vector_record(
                payload["user_id"], payload["vector_id"]
            )

        def memory_cleanup_vectors(_user_id, payload, _decision):
            if not self.memory_repository:
                raise RuntimeError("记忆仓储未初始化")
            return self.memory_repository.cleanup_orphan_vectors(payload["user_id"])

        return {
            "memory_delete": memory_delete,
            "memory_update": memory_update,
            "memory_clear": memory_clear,
            "memory_save_summary": memory_save_summary,
            "memory_sync_markdown": memory_sync_markdown,
            "memory_update_vector": memory_update_vector,
            "memory_delete_vector": memory_delete_vector,
            "memory_cleanup_vectors": memory_cleanup_vectors,
        }

    def _graph(self, action_type: str) -> ActionApprovalGraph:
        if action_type not in self._graphs:
            handlers = self._handlers()
            if action_type not in handlers:
                raise ValueError(f"不支持的敏感操作: {action_type}")
            handler = handlers[action_type]

            def execute(user_id, resolved_type, payload, decision):
                idempotency_key = decision.get("idempotency_key") or payload.get("idempotency_key")
                if idempotency_key and idempotency_key in self._receipts:
                    return self._receipts[idempotency_key]
                result = handler(user_id, payload, decision)
                receipt = {
                    "action_type": resolved_type,
                    "idempotency_key": idempotency_key,
                    "result": result,
                }
                if idempotency_key:
                    self._receipts[idempotency_key] = receipt
                return receipt

            self._graphs[action_type] = ActionApprovalGraph(
                execute,
                action_type=action_type,
                approved_actions=("approve",),
                checkpointer=create_checkpointer("memory"),
            )
        return self._graphs[action_type]

    def start(
        self,
        action_type: str,
        user_id: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        approval_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        approval_id = approval_id or uuid4().hex
        self._approval_types[approval_id] = action_type
        return self._graph(action_type).start(
            approval_id,
            user_id,
            payload or {},
            action_type=action_type,
        )

    def resume(self, approval_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
        action_type = self._approval_types.get(approval_id)
        if not action_type:
            raise ValueError("找不到待审批操作")
        result = self._graph(action_type).resume(approval_id, decision)
        if result.get("status") != "awaiting_approval":
            self._approval_types.pop(approval_id, None)
        return result

    def pending_action(self, approval_id: str) -> Optional[str]:
        return self._approval_types.get(approval_id)
