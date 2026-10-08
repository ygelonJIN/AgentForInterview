"""
长期记忆 - 向量数据库存储
"""
import os
import json
import re
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.observability import log_warning


class LongTermMemory:
    """长期记忆管理器"""

    def __init__(self, persist_dir: str = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if persist_dir is None:
            persist_dir = os.path.join(base_dir, "memory_db")

        self.persist_dir = persist_dir
        self._memory_store: Dict[str, List[Dict]] = {}
        self.embeddings = None
        self.vectorstore = None
        self.init_error = ""
        self._initialize()

    def _initialize(self):
        """初始化向量数据库"""
        try:
            from app.config import create_embeddings
            from langchain_chroma import Chroma

            self.embeddings = create_embeddings()
            self.vectorstore = Chroma(
                collection_name="user_memories",
                embedding_function=self.embeddings,
                persist_directory=self.persist_dir,
            )
        except Exception as e:
            self.init_error = f"{type(e).__name__}: {str(e)[:300]}"
            log_warning("memory.long_term_init", self.init_error)
            # 仅作为测试/开发降级；MemoryRepository 会拒绝把降级状态伪装成持久化成功。
            self._memory_store: Dict[str, List[Dict]] = {}

    def save_summary(
        self,
        user_id: str,
        summary: str,
        metadata: Dict = None,
        memory_id: str = None,
    ):
        """保存对话摘要"""
        memory_id = memory_id or str(uuid.uuid4())
        meta = {
            "type": "summary",
            "memory_id": memory_id,
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
            **(metadata or {}),
        }

        if self.vectorstore:
            self.vectorstore.add_texts(
                texts=[summary],
                metadatas=[meta],
                ids=[memory_id],
            )
        else:
            if user_id not in self._memory_store:
                self._memory_store[user_id] = []
            self._memory_store[user_id].append({
                "id": memory_id,
                "text": summary,
                "metadata": meta,
            })

    def upsert_memory(
        self,
        user_id: str,
        memory_id: str,
        content: str,
        metadata: Dict = None,
    ):
        """按稳定 memory_id 更新向量记忆。"""
        self.delete_memory(memory_id, user_id=user_id)
        self.save_summary(user_id, content, metadata=metadata, memory_id=memory_id)

    @property
    def is_persistent(self) -> bool:
        return self.vectorstore is not None

    def update_vector_by_id(
        self,
        vector_id: str,
        content: str,
        user_id: str = None,
    ) -> bool:
        """只更新一条孤立向量，不创建对应 MD。"""
        if self.vectorstore:
            try:
                collection = self.vectorstore._collection
                result = collection.get(ids=[vector_id], include=["metadatas"])
                ids = list(result.get("ids") or [])
                if not ids:
                    return False
                metadata = (result.get("metadatas") or [{}])[0] or {}
                if user_id is not None and metadata.get("user_id") != user_id:
                    return False
                embedding = self.embeddings.embed_documents([content])[0]
                collection.update(
                    ids=[vector_id],
                    documents=[content],
                    embeddings=[embedding],
                    metadatas=[metadata],
                )
                return True
            except Exception:
                return False

        memories = self._memory_store.get(user_id, [])
        changed = False
        for item in memories:
            item_id = item.get("id") or item.get("metadata", {}).get("vector_id")
            if item_id == vector_id:
                item["text"] = content
                changed = True
        return changed

    def delete_vector_by_id(self, vector_id: str, user_id: str = None) -> bool:
        """删除单条向量；可选校验归属用户。"""
        if self.vectorstore:
            try:
                collection = self.vectorstore._collection
                result = collection.get(ids=[vector_id], include=["metadatas"])
                ids = list(result.get("ids") or [])
                if not ids:
                    return False
                metadata = (result.get("metadatas") or [{}])[0] or {}
                if user_id is not None and metadata.get("user_id") != user_id:
                    return False
                collection.delete(ids=[vector_id])
                return True
            except Exception:
                return False

        memories = self._memory_store.get(user_id, [])
        remaining = [
            item for item in memories
            if item.get("id") != vector_id and item.get("metadata", {}).get("vector_id") != vector_id
        ]
        changed = len(remaining) != len(memories)
        self._memory_store[user_id] = remaining
        return changed

    def delete_memory(self, memory_id: str, user_id: str = None) -> bool:
        """按稳定 memory_id 删除向量记忆。"""
        if self.vectorstore:
            try:
                collection = self.vectorstore._collection
                result = collection.get(where={"memory_id": memory_id}, include=["metadatas"])
                ids = []
                for item_id, metadata in zip(result.get("ids", []), result.get("metadatas", [])):
                    if user_id is None or metadata.get("user_id") == user_id:
                        ids.append(item_id)
                if ids:
                    collection.delete(ids=ids)
                    return True
            except Exception:
                return False
            return False

        if user_id is not None:
            memories = self._memory_store.get(user_id, [])
            remaining = [
                item for item in memories
                if item.get("id") != memory_id and item.get("metadata", {}).get("memory_id") != memory_id
            ]
            self._memory_store[user_id] = remaining
            return len(remaining) != len(memories)

        deleted = False
        for key in list(self._memory_store):
            memories = self._memory_store[key]
            remaining = [
                item for item in memories
                if item.get("id") != memory_id and item.get("metadata", {}).get("memory_id") != memory_id
            ]
            deleted = deleted or len(remaining) != len(memories)
            self._memory_store[key] = remaining
        return deleted

    def delete_user_vectors_not_in(self, user_id: str, keep_memory_ids) -> int:
        """删除用户向量记忆中不在 keep_memory_ids 的条目。"""
        keep = {str(item) for item in keep_memory_ids if item}
        if self.vectorstore:
            try:
                collection = self.vectorstore._collection
                result = collection.get(where={"user_id": user_id}, include=["metadatas"])
                ids = []
                for item_id, metadata in zip(result.get("ids", []), result.get("metadatas", [])):
                    memory_id = str((metadata or {}).get("memory_id", ""))
                    if not memory_id or memory_id not in keep:
                        ids.append(item_id)
                if ids:
                    collection.delete(ids=ids)
                return len(ids)
            except Exception:
                return 0

        memories = self._memory_store.get(user_id, [])
        remaining = []
        deleted = 0
        for item in memories:
            metadata = item.get("metadata", {})
            memory_id = str(item.get("id") or metadata.get("memory_id") or "")
            if not memory_id or memory_id not in keep:
                deleted += 1
            else:
                remaining.append(item)
        self._memory_store[user_id] = remaining
        return deleted

    def save_event(self, user_id: str, event_type: str, event_data: Dict[str, Any]):
        """保存重要事件"""
        text = (
            f"用户{event_data.get('action', '操作')}了"
            f"{event_data.get('object', '')}，时间"
            f"{event_data.get('time', datetime.now().strftime('%Y-%m-%d'))}"
        )
        meta = {
            "type": "event",
            "event_type": event_type,
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
            **event_data,
        }

        if self.vectorstore:
            self.vectorstore.add_texts(texts=[text], metadatas=[meta])
        else:
            if user_id not in self._memory_store:
                self._memory_store[user_id] = []
            self._memory_store[user_id].append({"text": text, "metadata": meta})

    def recall(self, user_id: str, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """根据查询召回相关记忆"""
        if self.vectorstore:
            try:
                filter_dict = {"user_id": user_id}
                results = self.vectorstore.similarity_search(query, k=k, filter=filter_dict)
                return [
                    {
                        "content": doc.page_content,
                        "metadata": dict(doc.metadata or {}),
                    }
                    for doc in results
                ]
            except Exception as exc:
                # 向量召回依赖在线 Embedding。网络或 Embedding 服务不可用时，
                # 不能让已经持久化的用户 Memory 整体消失，改为本地可验证召回。
                log_warning(
                    "memory.vector_recall_fallback",
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                    {"user_id": user_id},
                )
                memories = self.list_user_memories(user_id, limit=max(k * 4, 20))
                return self._local_recall(memories, query, k)
        else:
            memories = self._memory_store.get(user_id, [])
            return self._local_recall(
                [
                    {"content": mem.get("text", ""), "metadata": mem.get("metadata", {})}
                    for mem in memories
                ],
                query,
                k,
            )

    @staticmethod
    def _local_recall(
        memories: List[Dict[str, Any]],
        query: str,
        k: int,
    ) -> List[Dict[str, Any]]:
        """不依赖 Embedding 的中文字符二元组召回。"""
        def ngrams(value: str) -> set[str]:
            normalized = re.sub(r"\s+", "", str(value or "")).casefold()
            return {
                normalized[index:index + 2]
                for index in range(max(0, len(normalized) - 1))
            }

        query_terms = ngrams(query)
        scored = []
        for index, memory in enumerate(memories):
            content = str(memory.get("content") or memory.get("text") or "")
            overlap = len(query_terms & ngrams(content))
            metadata = memory.get("metadata") or {}
            keep_as_profile = (
                metadata.get("category") == "general"
                or metadata.get("memory_type") == "preference"
            )
            scored.append((overlap, keep_as_profile, -index, memory))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

        # 用户画像通常条目很少；宁可带回明确的长期偏好，也不要因向量服务
        # 失败而漏掉性别、兴趣等与个性化直接相关的 Memory。
        selected = [
            item[3]
            for item in scored
            if item[0] > 0 or item[1]
        ][:k]
        if not selected and memories:
            selected = [item[3] for item in scored[:k]]
        normalized = []
        for memory in selected:
            item = dict(memory)
            item.setdefault("metadata", {})
            item["metadata"] = dict(item["metadata"])
            item["metadata"]["recall_source"] = "local_memory_fallback"
            content = item.pop("content", "")
            if not content:
                content = item.pop("text", "")
            normalized.append({"content": content, "metadata": item["metadata"]})
        return normalized

    def get_cross_scene_memories(
        self,
        user_id: str,
        current_scene: str,
        query: str = "",
    ) -> List[Dict[str, Any]]:
        """按当前问题检索跨场景记忆，避免固定查询召回无关内容。"""
        if current_scene == "travel":
            context_terms = "购物 装备 商品 购买 户外"
        elif current_scene == "shopping":
            context_terms = "旅行 目的地 景点 活动 户外"
        else:
            context_terms = ""
        retrieval_query = f"{query} {context_terms}".strip()
        return self.recall(user_id, retrieval_query, k=5)

    def list_user_memories(self, user_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """完整列出用户的向量记忆，供记忆中心人工检查。"""
        if self.vectorstore:
            try:
                collection = self.vectorstore._collection
                result = collection.get(
                    where={"user_id": user_id},
                    include=["documents", "metadatas"],
                )
                memories = []
                for item_id, content, metadata in zip(
                    result.get("ids", []),
                    result.get("documents", []) or [],
                    result.get("metadatas", []) or [],
                ):
                    metadata = dict(metadata or {})
                    memories.append({
                        "id": item_id,
                        "content": content or metadata.get("chroma:document", ""),
                        "metadata": metadata,
                    })
            except Exception:
                memories = []
        else:
            memories = [
                {
                    "id": item.get("id") or item.get("metadata", {}).get("memory_id"),
                    "content": item.get("text", ""),
                    "metadata": dict(item.get("metadata", {})),
                }
                for item in self._memory_store.get(user_id, [])
            ]

        memories.sort(
            key=lambda item: str(item.get("metadata", {}).get("timestamp", "")),
            reverse=True,
        )
        return memories[:max(0, int(limit))]

    def get_memory_count(self, user_id: str) -> int:
        """返回用户向量记忆数量，不触发远程 Embedding 请求。"""
        if self.vectorstore:
            try:
                result = self.vectorstore._collection.get(
                    where={"user_id": user_id},
                    include=["metadatas"],
                )
                return len(result.get("ids", []))
            except Exception:
                return 0
        return len(self._memory_store.get(user_id, []))

    def get_user_profile(self, user_id: str) -> Dict[str, Any]:
        """获取用户画像（从记忆中汇总）"""
        memories = self.recall(user_id, "偏好 喜欢 爱好", k=10)
        events = self.recall(user_id, "购买 旅行 事件", k=10)

        return {
            "user_id": user_id,
            "memory_count": len(memories) + len(events),
            "recent_memories": memories[:5],
            "recent_events": events[:5],
        }
