"""MD 文档和向量记忆的统一 CRUD 仓储。"""
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional


class MemoryRepository:
    """以 MD/index 为权威记录，并同步向量索引。"""

    def __init__(self, md_memory, long_term_memory):
        self.md_memory = md_memory
        self.long_term_memory = long_term_memory

    def save_preference(
        self,
        user_id: str,
        content: str,
        category: str = "general",
    ) -> str:
        duplicate_id = self._find_duplicate(user_id, content)
        if duplicate_id:
            return duplicate_id
        memory_id = self.md_memory.save_preference(user_id, content, category)
        self._sync_vector(user_id, memory_id, content, "preference", category, version=1)
        return memory_id

    def save_event(
        self,
        user_id: str,
        content: str,
        event_type: str = "general",
    ) -> str:
        duplicate_id = self._find_duplicate(user_id, content)
        if duplicate_id:
            return duplicate_id
        memory_id = self.md_memory.save_event(user_id, content, event_type)
        self._sync_vector(user_id, memory_id, content, "event", event_type, version=1)
        return memory_id

    def update_memory(self, user_id: str, memory_id: str, new_content: str) -> bool:
        updated = self.md_memory.update_memory(user_id, memory_id, new_content)
        if updated:
            memory = self._find_memory(user_id, memory_id)
            memory_type = memory.get("type", "preference") if memory else "preference"
            category = memory.get("category", memory.get("event_type", "general")) if memory else "general"
            self._sync_vector(
                user_id,
                memory_id,
                new_content,
                memory_type,
                category,
                version=(memory.get("version", 2) if memory else 2),
            )
        return updated

    def delete_memory(self, user_id: str, memory_id: str) -> bool:
        deleted = self.md_memory.delete_memory(user_id, memory_id)
        if deleted and self.long_term_memory:
            self.long_term_memory.delete_memory(memory_id, user_id=user_id)
        return deleted

    def clear_user_memories(self, user_id: str) -> int:
        """删除用户的 MD 记忆并同步清理向量索引。"""
        memories = self.md_memory.get_all_memories(user_id)
        deleted_count = 0
        for memory in memories:
            memory_id = memory.get("id")
            if memory_id and self.delete_memory(user_id, memory_id):
                deleted_count += 1
        return deleted_count

    def save_summary(self, user_id: str, summary: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        memory_id = self.md_memory.save_preference(user_id, summary, "compressed_summary")
        self._sync_vector(
            user_id,
            memory_id,
            summary,
            "preference",
            "compressed_summary",
            version=1,
            extra=metadata,
        )
        return memory_id

    def _find_memory(self, user_id: str, memory_id: str) -> Optional[Dict[str, Any]]:
        for memory in self.md_memory.get_all_memories(user_id):
            if memory.get("id") == memory_id:
                return memory
        return None

    @staticmethod
    def _normalize_content(content: str) -> str:
        return re.sub(r"\s+", "", str(content or "")).casefold()

    def _find_duplicate(self, user_id: str, content: str) -> Optional[str]:
        """写入前按 MD 权威记录二次查重。"""
        candidate = self._normalize_content(content)
        if not candidate:
            return None
        for memory in self.md_memory.get_all_memories(user_id):
            previous = self._normalize_content(memory.get("content", ""))
            if not previous:
                continue
            if previous == candidate or SequenceMatcher(None, candidate, previous).ratio() >= 0.92:
                return memory.get("id")
        return None

    def _sync_vector(
        self,
        user_id: str,
        memory_id: str,
        content: str,
        memory_type: str,
        category: str,
        version: int,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.long_term_memory:
            return
        metadata = {
            "memory_id": memory_id,
            "memory_type": memory_type,
            "category": category,
            "source": "md_memory",
            "version": version,
            **(extra or {}),
        }
        self.long_term_memory.upsert_memory(user_id, memory_id, content, metadata=metadata)


def get_memory_repository() -> MemoryRepository:
    """创建 UI/CLI 共用的 MD + 向量仓储。"""
    from app.memory.long_term import LongTermMemory
    from app.memory.md_memory import MDMemory

    return MemoryRepository(MDMemory(), LongTermMemory())
