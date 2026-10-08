import re

from app.memory.long_term import LongTermMemory
from app.memory.md_memory import MDMemory
from app.memory.repository import MemoryRepository


class VectorSpy:
    def __init__(self):
        self.upserts = []
        self.deletes = []
        self.orphan_deleted = []

    def upsert_memory(self, user_id, memory_id, content, metadata=None):
        self.upserts.append((user_id, memory_id, content, metadata))

    def delete_memory(self, memory_id, user_id=None):
        self.deletes.append((memory_id, user_id))
        return True

    def delete_user_vectors_not_in(self, user_id, keep_ids):
        self.orphan_deleted.append((user_id, set(keep_ids)))
        return 2


def test_manual_markdown_edit_add_and_delete_sync_vectors(tmp_path):
    md = MDMemory(base_dir=str(tmp_path))
    vector = VectorSpy()
    repository = MemoryRepository(md, vector)

    first_id = repository.save_preference("user-1", "旧偏好", "travel")
    preferences_path = tmp_path / "user-1" / "preferences.md"
    content = preferences_path.read_text(encoding="utf-8").replace("旧偏好", "新偏好")
    content += "\n## 手工新增偏好 - 2026-10-06 12:00\n\n喜欢清晨徒步\n"
    preferences_path.write_text(content, encoding="utf-8")

    stats = repository.sync_from_markdown("user-1")

    assert stats["updated"] == 1
    assert stats["added"] == 1
    assert stats["removed"] == 0
    assert len(vector.upserts) == 3  # save + update + manual add
    assert vector.upserts[1][1] == first_id
    assert vector.upserts[1][2] == "新偏好"
    assert vector.upserts[1][3]["version"] == 2
    assert vector.upserts[2][2] == "喜欢清晨徒步"
    assert vector.upserts[2][1]

    manual_id = vector.upserts[2][1]
    content = preferences_path.read_text(encoding="utf-8")
    content = re.sub(rf"## \[{manual_id}\].*?(?=## \[|$)", "", content, flags=re.DOTALL)
    preferences_path.write_text(content, encoding="utf-8")

    stats = repository.sync_from_markdown("user-1")
    assert stats["removed"] == 1
    assert vector.deletes == [(manual_id, "user-1")]


def test_repository_can_cleanup_legacy_orphan_vectors(tmp_path):
    md = MDMemory(base_dir=str(tmp_path))
    vector = VectorSpy()
    repository = MemoryRepository(md, vector)
    kept_id = repository.save_preference("user-1", "保留", "general")

    result = repository.cleanup_orphan_vectors("user-1")

    assert result == {"deleted": 2, "valid_ids": [kept_id]}
    assert vector.orphan_deleted == [("user-1", {kept_id})]


def test_free_text_at_end_is_autofmtted_into_new_memory(tmp_path):
    md = MDMemory(base_dir=str(tmp_path))
    original = "# 用户偏好\n\n## [old-id] general - 2026-10-06 10:00\n\n已有偏好\n"
    edited = original + "\n喜欢清晨徒步\n\n喜欢轻量装备\n"

    formatted = md.autofmt_document("preferences", edited, original)

    assert formatted.count("## [") == 3
    assert "喜欢清晨徒步" in formatted
    assert "喜欢轻量装备" in formatted
    assert formatted != edited


class NonPersistentVector:
    is_persistent = False
    init_error = "Embedding 未配置"


def test_repository_rejects_fake_persistent_success():
    md = MDMemory(base_dir="/tmp/memory-repository-should-not-write")
    repository = MemoryRepository(md, NonPersistentVector())

    try:
        repository.save_preference("user-x", "喜欢徒步")
    except RuntimeError as exc:
        assert "无法持久化" in str(exc)
    else:
        raise AssertionError("非持久化向量库不应保存成功")


class FlakyPersistentVector:
    is_persistent = True

    def __init__(self):
        self.calls = 0
        self.saved = []

    def upsert_memory(self, user_id, memory_id, content, metadata=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Embedding 临时失败")
        self.saved.append((user_id, memory_id, content, metadata))

    def delete_memory(self, memory_id, user_id=None):
        return True

    def list_user_memories(self, user_id, limit=200):
        return []


def test_failed_vector_sync_is_retried_on_next_markdown_sync(tmp_path):
    md = MDMemory(base_dir=str(tmp_path))
    vector = FlakyPersistentVector()
    repository = MemoryRepository(md, vector)

    first = repository.replace_markdown_documents(
        "user-1",
        "# 用户偏好\n\n喜欢徒步\n",
        "# 事件记录\n",
    )
    assert first["errors"]
    assert vector.saved == []

    second = repository.sync_from_markdown("user-1")

    assert second["errors"] == []
    assert len(vector.saved) == 1
    assert vector.saved[0][2] == "喜欢徒步"
