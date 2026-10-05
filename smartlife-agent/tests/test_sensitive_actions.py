"""记忆敏感操作审批服务测试。"""

import pytest

from app.sensitive_actions import SensitiveActionService


class _MemoryRepository:
    def __init__(self):
        self.deleted = []
        self.updated = []
        self.cleared = []
        self.summaries = []

    def delete_memory(self, user_id, memory_id):
        self.deleted.append((user_id, memory_id))
        return True

    def update_memory(self, user_id, memory_id, content):
        self.updated.append((user_id, memory_id, content))
        return True

    def clear_user_memories(self, user_id):
        self.cleared.append(user_id)
        return 3

    def save_summary(self, user_id, summary, metadata=None):
        self.summaries.append((user_id, summary, metadata))
        return "summary-1"


def test_memory_delete_requires_approval_before_side_effect():
    repository = _MemoryRepository()
    service = SensitiveActionService(memory_repository=repository)

    pending = service.start(
        "memory_delete",
        "user-1",
        {"user_id": "user-1", "memory_id": "m-1", "idempotency_key": "delete-1"},
    )

    assert pending["status"] == "awaiting_approval"
    assert repository.deleted == []

    result = service.resume(pending["approval_id"], {
        "action": "approve",
        "idempotency_key": "delete-1",
    })

    assert result["status"] == "executed"
    assert result["state"]["result"]["result"] == {"deleted": True}
    assert repository.deleted == [("user-1", "m-1")]


def test_sensitive_action_reject_does_not_execute():
    repository = _MemoryRepository()
    service = SensitiveActionService(memory_repository=repository)

    pending = service.start("memory_clear", "user-1", {"idempotency_key": "clear-1"})
    result = service.resume(pending["approval_id"], {"action": "reject"})

    assert result["status"] == "rejected"
    assert repository.cleared == []


def test_sensitive_action_idempotency_key_reuses_receipt():
    repository = _MemoryRepository()
    service = SensitiveActionService(memory_repository=repository)
    first = service.start("memory_save_summary", "user-1", {
        "user_id": "user-1",
        "summary": "摘要",
        "idempotency_key": "summary-1",
    })
    second = service.start("memory_save_summary", "user-1", {
        "user_id": "user-1",
        "summary": "摘要",
        "idempotency_key": "summary-1",
    })

    first_result = service.resume(first["approval_id"], {
        "action": "approve",
        "idempotency_key": "summary-1",
    })
    second_result = service.resume(second["approval_id"], {
        "action": "approve",
        "idempotency_key": "summary-1",
    })

    assert first_result["state"]["result"]["result"] == {"memory_id": "summary-1"}
    assert second_result["state"]["result"] == first_result["state"]["result"]
    assert len(repository.summaries) == 1


def test_unknown_sensitive_action_is_rejected():
    with pytest.raises(ValueError, match="不支持"):
        SensitiveActionService().start("unknown", "user-1", {})
