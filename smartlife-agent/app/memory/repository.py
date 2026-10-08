"""MD 文档和向量记忆的统一 CRUD 仓储。"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional


class MemoryRepository:
    """以 MD/index 为权威记录，并同步向量索引。"""

    def __init__(self, md_memory, long_term_memory):
        self.md_memory = md_memory
        self.long_term_memory = long_term_memory

    def _require_vector_sync(self) -> None:
        """禁止把 Embedding/Chroma 初始化失败伪装成持久化成功。"""
        if not self.long_term_memory:
            raise RuntimeError("长期向量记忆未初始化")
        if hasattr(self.long_term_memory, "is_persistent") and not self.long_term_memory.is_persistent:
            detail = getattr(self.long_term_memory, "init_error", "") or "向量库不可用"
            raise RuntimeError(f"向量记忆无法持久化：{detail}")

    def save_preference(
        self,
        user_id: str,
        content: str,
        category: str = "general",
    ) -> str:
        self._require_vector_sync()
        duplicate_id = self._find_duplicate(user_id, content)
        if duplicate_id:
            return duplicate_id
        memory_id = self.md_memory.save_preference(user_id, content, category)
        try:
            self._sync_vector(user_id, memory_id, content, "preference", category, version=1)
        except Exception:
            self.md_memory.delete_memory(user_id, memory_id)
            raise
        return memory_id

    def save_event(
        self,
        user_id: str,
        content: str,
        event_type: str = "general",
    ) -> str:
        self._require_vector_sync()
        duplicate_id = self._find_duplicate(user_id, content)
        if duplicate_id:
            return duplicate_id
        memory_id = self.md_memory.save_event(user_id, content, event_type)
        try:
            self._sync_vector(user_id, memory_id, content, "event", event_type, version=1)
        except Exception:
            self.md_memory.delete_memory(user_id, memory_id)
            raise
        return memory_id

    def update_memory(self, user_id: str, memory_id: str, new_content: str) -> bool:
        self._require_vector_sync()
        previous = self._find_memory(user_id, memory_id)
        previous_content = previous.get("content", "") if previous else ""
        updated = self.md_memory.update_memory(user_id, memory_id, new_content)
        if updated:
            memory = self._find_memory(user_id, memory_id)
            memory_type = memory.get("type", "preference") if memory else "preference"
            category = memory.get("category", memory.get("event_type", "general")) if memory else "general"
            try:
                self._sync_vector(
                    user_id,
                    memory_id,
                    new_content,
                    memory_type,
                    category,
                    version=(memory.get("version", 2) if memory else 2),
                )
            except Exception:
                if previous_content:
                    self.md_memory.update_memory(user_id, memory_id, previous_content)
                raise
        return updated

    def delete_memory(self, user_id: str, memory_id: str) -> bool:
        deleted = self.md_memory.delete_memory(user_id, memory_id)
        if deleted and self.long_term_memory:
            self.long_term_memory.delete_memory(memory_id, user_id=user_id)
        return deleted

    def delete_vector_record(self, user_id: str, vector_id: str) -> Dict[str, Any]:
        """删除单条向量；有 memory_id 时同时删除对应 MD/index。"""
        self._require_vector_sync()
        memories = self.long_term_memory.list_user_memories(user_id, limit=1000)
        target = next((item for item in memories if item.get("id") == vector_id), None)
        if not target:
            return {"deleted": False, "reason": "向量不存在或不属于当前用户"}
        memory_id = (target.get("metadata") or {}).get("memory_id")
        if memory_id:
            deleted = self.delete_memory(user_id, memory_id)
            return {
                "deleted": deleted,
                "memory_id": memory_id,
                "vector_id": vector_id,
                "md_deleted": deleted,
            }
        deleted = self.long_term_memory.delete_vector_by_id(vector_id, user_id=user_id)
        return {
            "deleted": deleted,
            "memory_id": None,
            "vector_id": vector_id,
            "md_deleted": False,
        }

    def clear_user_memories(self, user_id: str) -> int:
        """删除用户的 MD 记忆并同步清理向量索引。"""
        memories = self.md_memory.get_all_memories(user_id)
        deleted_count = 0
        for memory in memories:
            memory_id = memory.get("id")
            if memory_id and self.delete_memory(user_id, memory_id):
                deleted_count += 1
        return deleted_count

    def save_summary(
        self,
        user_id: str,
        summary: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        self._require_vector_sync()
        duplicate_id = self._find_duplicate(user_id, summary)
        if duplicate_id:
            return duplicate_id
        memory_id = self.md_memory.save_preference(user_id, summary, "compressed_summary")
        try:
            self._sync_vector(
                user_id,
                memory_id,
                summary,
                "preference",
                "compressed_summary",
                version=1,
                extra=metadata,
            )
        except Exception:
            self.md_memory.delete_memory(user_id, memory_id)
            raise
        return memory_id

    def replace_markdown_documents(
        self,
        user_id: str,
        preferences_content: str,
        events_content: str,
    ) -> Dict[str, Any]:
        """人工编辑完整 MD 后，统一同步 index.json 和向量记忆。"""
        self._require_vector_sync()
        self.md_memory.write_raw_document(user_id, "preferences", preferences_content)
        self.md_memory.write_raw_document(user_id, "events", events_content)
        return self.sync_from_markdown(user_id)

    def sync_from_markdown(self, user_id: str) -> Dict[str, Any]:
        """以 MD 原文为权威源，对账新增、修改和删除的向量记忆。"""
        self._require_vector_sync()
        previous = {
            item.get("id"): item
            for item in self.md_memory.get_all_memories(user_id)
            if item.get("id")
        }
        stats = self.md_memory.reconcile_index_from_markdown(user_id)
        current = {
            item.get("id"): item
            for item in self.md_memory.get_all_memories(user_id)
            if item.get("id")
        }

        upserted = []
        deleted = []
        errors = []
        for memory_id, item in current.items():
            before = previous.get(memory_id)
            target_version = int(item.get("version", 1))
            if (
                before
                and before.get("content") == item.get("content")
                and int(before.get("vector_version", 0)) == target_version
            ):
                continue
            category = item.get("category", item.get("event_type", "general"))
            try:
                self._sync_vector(
                    user_id,
                    memory_id,
                    item.get("content", ""),
                    item.get("type", "preference"),
                    category,
                    target_version,
                )
                self.md_memory.mark_vector_synced(user_id, memory_id, target_version)
                upserted.append(memory_id)
            except Exception as exc:
                errors.append(f"{memory_id}: {type(exc).__name__}: {exc}")

        for memory_id in previous:
            if memory_id in current:
                continue
            try:
                if self.long_term_memory:
                    self.long_term_memory.delete_memory(memory_id, user_id=user_id)
                deleted.append(memory_id)
            except Exception as exc:
                errors.append(f"{memory_id}: {type(exc).__name__}: {exc}")

        stats.update({
            "upserted": upserted,
            "deleted_vectors": deleted,
            "errors": errors,
        })
        return stats

    def update_vector_record(
        self,
        user_id: str,
        vector_id: str,
        content: str,
    ) -> Dict[str, Any]:
        """编辑单条向量；有 memory_id 时同步更新 MD。"""
        self._require_vector_sync()
        memories = self.long_term_memory.list_user_memories(user_id, limit=1000)
        target = next((item for item in memories if item.get("id") == vector_id), None)
        if not target:
            return {"updated": False, "reason": "向量不存在或不属于当前用户"}
        memory_id = (target.get("metadata") or {}).get("memory_id")
        if memory_id:
            updated = self.update_memory(user_id, memory_id, content)
            return {
                "updated": updated,
                "memory_id": memory_id,
                "vector_id": vector_id,
                "md_updated": updated,
            }
        updated = self.long_term_memory.update_vector_by_id(
            vector_id, user_id=user_id, content=content
        )
        return {
            "updated": updated,
            "memory_id": None,
            "vector_id": vector_id,
            "md_updated": False,
        }

    def cleanup_orphan_vectors(self, user_id: str) -> Dict[str, Any]:
        """删除不属于当前 MD/index 的向量记忆，包括旧版无 memory_id 记录。"""
        self._require_vector_sync()
        valid_ids = {
            item.get("id")
            for item in self.md_memory.get_all_memories(user_id)
            if item.get("id")
        }
        deleted = self.long_term_memory.delete_user_vectors_not_in(user_id, valid_ids)
        return {"deleted": deleted, "valid_ids": sorted(valid_ids)}

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
        self._require_vector_sync()
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
