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


class _VectorWithListing(_Vector):
    def __init__(self, records):
        super().__init__()
        self.records = records

    def list_user_memories(self, user_id, limit=200):
        return self.records

    def delete_vector_by_id(self, vector_id, user_id=None):
        self.deleted.append((vector_id, user_id))
        return True


def test_delete_vector_record_removes_corresponding_md():
    class _MDWithDelete(_MD):
        def __init__(self):
            super().__init__()
            self.memories = [{"id": "m-1", "content": "偏好"}]

    md = _MDWithDelete()
    vector = _VectorWithListing([{
        "id": "vector-1",
        "content": "偏好",
        "metadata": {"memory_id": "m-1"},
    }])
    repository = MemoryRepository(md, vector)

    result = repository.delete_vector_record("user-1", "vector-1")

    assert result == {
        "deleted": True,
        "memory_id": "m-1",
        "vector_id": "vector-1",
        "md_deleted": True,
    }
    assert md.deleted == ["m-1"]
    assert vector.deleted == [("m-1", "user-1")]


class _UpdateVector(_Vector):
    def __init__(self, records):
        super().__init__()
        self.records = records
        self.updated = []

    def list_user_memories(self, user_id, limit=200):
        return self.records

    def update_vector_by_id(self, vector_id, content, user_id=None):
        self.updated.append((vector_id, content, user_id))
        return True


def test_update_orphan_vector_does_not_require_md():
    md = _MD()
    vector = _UpdateVector([{
        "id": "vector-9",
        "content": "旧内容",
        "metadata": {"user_id": "user-1"},
    }])
    repository = MemoryRepository(md, vector)

    result = repository.update_vector_record("user-1", "vector-9", "新内容")

    assert result == {
        "updated": True,
        "memory_id": None,
        "vector_id": "vector-9",
        "md_updated": False,
    }
    assert vector.updated == [("vector-9", "新内容", "user-1")]
