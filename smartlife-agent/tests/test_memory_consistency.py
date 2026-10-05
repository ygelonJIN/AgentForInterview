"""MD 记忆与向量索引一致性测试。"""

from app.memory.long_term import LongTermMemory
from app.memory.md_memory import MDMemory
from app.memory.repository import MemoryRepository


class _VectorSpy:
    def __init__(self):
        self.upserts = []
        self.deletes = []

    def upsert_memory(self, user_id, memory_id, content, metadata=None):
        self.upserts.append((user_id, memory_id, content, metadata))

    def delete_memory(self, memory_id, user_id=None):
        self.deletes.append((memory_id, user_id))
        return True


def test_repository_keeps_stable_memory_id_across_save_update_delete(tmp_path):
    md = MDMemory(base_dir=str(tmp_path))
    vector = _VectorSpy()
    repository = MemoryRepository(md, vector)

    memory_id = repository.save_preference("user-1", "喜欢跑步", "general")

    assert vector.upserts[0][1] == memory_id
    assert vector.upserts[0][3]["memory_type"] == "preference"
    assert vector.upserts[0][3]["version"] == 1

    assert repository.update_memory("user-1", memory_id, "喜欢慢跑") is True
    assert md.get_memory_content("user-1", memory_id) == "喜欢慢跑"
    assert vector.upserts[1][1] == memory_id
    assert vector.upserts[1][3]["version"] == 2

    assert repository.delete_memory("user-1", memory_id) is True
    assert vector.deletes == [(memory_id, "user-1")]
    assert md.get_memory_content("user-1", memory_id) is None


def test_long_term_memory_fallback_uses_memory_id_for_update_and_delete():
    memory = LongTermMemory.__new__(LongTermMemory)
    memory.vectorstore = None
    memory._memory_store = {}

    memory.save_summary("user-1", "旧内容", {"memory_type": "preference"}, memory_id="stable-id")
    memory.upsert_memory("user-1", "stable-id", "新内容", {"memory_type": "preference"})

    assert [item["content"] for item in memory.recall("user-1", "内容")] == ["新内容"]
    assert memory.delete_memory("stable-id", user_id="user-1") is True
    assert memory.recall("user-1", "内容") == []


def test_cross_scene_memory_recall_uses_current_query():
    memory = LongTermMemory.__new__(LongTermMemory)
    memory.vectorstore = None
    memory._memory_store = {
        "user-1": [
            {"text": "用户喜欢杭州西湖", "metadata": {"memory_id": "a"}},
            {"text": "用户购买过帐篷", "metadata": {"memory_id": "b"}},
        ]
    }

    result = memory.get_cross_scene_memories("user-1", "shopping", query="杭州")

    assert result == [{"content": "用户喜欢杭州西湖", "metadata": {"memory_id": "a"}}]
