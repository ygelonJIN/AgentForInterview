"""记忆仓储清理测试。"""

from app.memory.repository import MemoryRepository


class _MD:
    def __init__(self):
        self.memories = [{"id": "m-1"}, {"id": "m-2"}]
        self.deleted = []

    def get_all_memories(self, user_id):
        return list(self.memories)

    def delete_memory(self, user_id, memory_id):
        self.deleted.append(memory_id)
        self.memories = [m for m in self.memories if m["id"] != memory_id]
        return True


class _Vector:
    def __init__(self):
        self.deleted = []

    def delete_memory(self, memory_id, user_id=None):
        self.deleted.append((memory_id, user_id))
        return True


def test_clear_user_memories_removes_md_and_vector_records():
    md = _MD()
    vector = _Vector()
    repository = MemoryRepository(md, vector)

    count = repository.clear_user_memories("user-1")

    assert count == 2
    assert md.deleted == ["m-1", "m-2"]
    assert vector.deleted == [("m-1", "user-1"), ("m-2", "user-1")]
