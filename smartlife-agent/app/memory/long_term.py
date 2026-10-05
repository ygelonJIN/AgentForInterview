"""
长期记忆 - 向量数据库存储
"""
import os
import json
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
            log_warning("memory.long_term_init", str(e))
            # 降级为内存字典
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
            filter_dict = {"user_id": user_id}
            results = self.vectorstore.similarity_search(query, k=k, filter=filter_dict)
            return [{"content": doc.page_content, "metadata": doc.metadata} for doc in results]
        else:
            memories = self._memory_store.get(user_id, [])
            if not query:
                return [{"content": mem["text"], "metadata": mem["metadata"]} for mem in memories[:k]]
            # 简单关键词匹配降级
            results = []
            for mem in memories:
                if any(word in mem["text"] for word in query.split()):
                    results.append({"content": mem["text"], "metadata": mem["metadata"]})
            return results[:k]

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
