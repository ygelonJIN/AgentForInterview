"""记忆工具契约：读取走记忆仓储，写入必须经过审批图。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Dict, List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.memory.approval_graph import MemoryApprovalGraph
from app.memory.long_term import LongTermMemory
from app.memory.md_memory import MDMemory
from app.memory.repository import MemoryRepository
from app.tools.tool_bundle import ToolBundle


class GetUserProfileInput(BaseModel):
    include_recent: bool = Field(default=True, description="是否包含最近记忆")


class SavePreferenceInput(BaseModel):
    preference_type: str = Field(description="偏好类型：shopping/travel/general")
    preference_data: Dict[str, Any] = Field(description="偏好数据")


class GetCrossSceneMemoriesInput(BaseModel):
    current_scene: str = Field(description="当前场景：shopping/travel")
    limit: int = Field(default=5, ge=1, le=50, description="返回记忆数量")
    query: str = Field(default="", description="当前问题，用于语义召回")


class SaveEventInput(BaseModel):
    event_type: str = Field(description="事件类型：purchase/travel/review")
    event_data: Dict[str, Any] = Field(description="事件数据")


class MemoryToolProvider(ToolBundle):
    """Memory 读取与审批写入工具提供方。"""

    def __init__(
        self,
        *,
        md_memory: Optional[MDMemory] = None,
        long_term_memory: Optional[LongTermMemory] = None,
        memory_repository: Optional[MemoryRepository] = None,
        approval_graph: Optional[MemoryApprovalGraph] = None,
        persist: Optional[Callable[..., Any]] = None,
        actor_id: Optional[str] = None,
    ):
        self.actor_id = str(actor_id or "").strip()
        self.md_memory = md_memory or MDMemory()
        self._long_term_memory = long_term_memory
        self._memory_repository = memory_repository
        self._persist = persist
        self._approval_graph = approval_graph
        super().__init__(
            name="memory-agent",
            description="记忆服务：用户画像、偏好、跨场景召回和审批写入",
        )

    def _require_actor(self) -> str:
        if not self.actor_id:
            raise PermissionError("记忆工具未绑定用户身份")
        return self.actor_id

    @property
    def long_term_memory(self):
        if self._long_term_memory is None:
            try:
                self._long_term_memory = LongTermMemory()
            except Exception:
                self._long_term_memory = None
        return self._long_term_memory

    @property
    def memory_repository(self):
        if self._memory_repository is None:
            self._memory_repository = MemoryRepository(self.md_memory, self.long_term_memory)
        return self._memory_repository

    @property
    def approval_graph(self):
        if self._approval_graph is None:
            self._approval_graph = MemoryApprovalGraph(self._persist_candidates)
        return self._approval_graph

    def _persist_candidates(self, user_id: str, candidates: List[Dict[str, Any]], selected_indices: List[int]):
        if self._persist is not None:
            return self._persist(user_id, candidates, selected_indices)
        saved_ids = []
        for index in selected_indices:
            if index < 0 or index >= len(candidates):
                continue
            item = candidates[index]
            content = str(item.get("content") or json.dumps(item.get("data", {}), ensure_ascii=False))
            category = str(item.get("category") or item.get("event_type") or "general")
            if item.get("type") == "event":
                saved_ids.append(self.memory_repository.save_event(user_id, content, category))
            else:
                saved_ids.append(self.memory_repository.save_preference(user_id, content, category))
        return saved_ids

    def _start_approval(
        self,
        user_id: str,
        candidate: Dict[str, Any],
        action_name: str,
    ) -> Dict[str, Any]:
        approval_id = f"{action_name}-{uuid.uuid4().hex}"
        result = self.approval_graph.start(approval_id, user_id, [candidate])
        return {
            "status": "approval_required",
            "approval_id": approval_id,
            "action": action_name,
            "user_id": user_id,
            "candidate": candidate,
            "interrupt": result.get("interrupt"),
        }

    def _initialize_tools(self):
        server = self

        @tool("get_user_profile", args_schema=GetUserProfileInput)
        def get_user_profile(include_recent: bool = True) -> Dict[str, Any]:
            """读取当前 Actor 的用户画像和最近记忆。"""
            actor_id = server._require_actor()
            profile = server.md_memory.get_user_profile(actor_id)
            return {**profile, "source": "md_memory", "include_recent": include_recent}

        @tool("save_preference", args_schema=SavePreferenceInput)
        def save_preference(
            preference_type: str,
            preference_data: Dict[str, Any],
        ) -> Dict[str, Any]:
            """创建偏好写入审批，不在审批前直接修改记忆。"""
            actor_id = server._require_actor()
            content = json.dumps(preference_data, ensure_ascii=False, sort_keys=True)
            return server._start_approval(
                actor_id,
                {
                    "type": "preference",
                    "category": preference_type,
                    "content": content,
                    "data": preference_data,
                },
                "save_preference",
            )

        @tool("get_cross_scene_memories", args_schema=GetCrossSceneMemoriesInput)
        def get_cross_scene_memories(
            current_scene: str,
            limit: int = 5,
            query: str = "",
        ) -> List[Dict[str, Any]]:
            """按当前问题召回当前 Actor 的跨场景记忆。"""
            actor_id = server._require_actor()
            memory = server.long_term_memory
            if memory is None:
                return []
            rows = memory.get_cross_scene_memories(actor_id, current_scene, query=query)
            return rows[:limit]

        @tool("save_event", args_schema=SaveEventInput)
        def save_event(
            event_type: str,
            event_data: Dict[str, Any],
        ) -> Dict[str, Any]:
            """创建事件写入审批，不在审批前直接修改记忆。"""
            actor_id = server._require_actor()
            content = json.dumps(event_data, ensure_ascii=False, sort_keys=True)
            return server._start_approval(
                actor_id,
                {
                    "type": "event",
                    "category": event_type,
                    "content": content,
                    "data": event_data,
                },
                "save_event",
            )

        @tool("get_user_context")
        def get_user_context() -> Dict[str, Any]:
            """读取当前 Actor 的用户画像和最近记忆摘要。"""
            actor_id = server._require_actor()
            profile = server.md_memory.get_user_profile(actor_id)
            return {
                "user_id": actor_id,
                "profile": profile,
                "source": "md_memory",
            }

        self.tools = [
            get_user_profile,
            save_preference,
            get_cross_scene_memories,
            save_event,
            get_user_context,
        ]
