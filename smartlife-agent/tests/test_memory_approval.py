"""LangGraph 候选记忆人工审批测试。"""

from app.memory.approval_graph import MemoryApprovalGraph


class _Persister:
    def __init__(self):
        self.calls = []

    def __call__(self, user_id, candidates, selected_indices):
        self.calls.append((user_id, [item["content"] for item in candidates], selected_indices))
        return [f"memory-{index}" for index in selected_indices]


def test_memory_approval_interrupts_then_saves_selected_candidates():
    persister = _Persister()
    graph = MemoryApprovalGraph(persister)
    candidates = [
        {"content": "喜欢跑步", "category": "general", "confidence": "high"},
        {"content": "预算1000元", "category": "shopping", "confidence": "medium"},
    ]

    pending = graph.start("approval-1", "user-1", candidates)

    assert pending["status"] == "awaiting_approval"
    assert pending["interrupt"]["approval_type"] == "memory"
    assert pending["interrupt"]["candidates"] == candidates
    assert persister.calls == []

    result = graph.resume("approval-1", {
        "action": "save_selected",
        "selected_indices": [1],
    })

    assert result["status"] == "saved"
    assert result["state"]["saved_ids"] == ["memory-1"]
    assert persister.calls == [("user-1", ["喜欢跑步", "预算1000元"], [1])]


def test_memory_approval_save_all_and_reject_are_terminal():
    persister = _Persister()
    graph = MemoryApprovalGraph(persister)
    candidates = [{"content": "事实A"}, {"content": "事实B"}]

    graph.start("approval-2", "user-2", candidates)
    saved = graph.resume("approval-2", {"action": "save_all"})

    assert saved["status"] == "saved"
    assert saved["state"]["saved_ids"] == ["memory-0", "memory-1"]
    assert persister.calls[-1][2] == [0, 1]

    graph.start("approval-3", "user-2", candidates)
    rejected = graph.resume("approval-3", {"action": "reject"})

    assert rejected["status"] == "rejected"
    assert rejected["state"]["saved_ids"] == []
    assert len(persister.calls) == 1


def test_memory_approval_invalid_decision_does_not_persist():
    persister = _Persister()
    graph = MemoryApprovalGraph(persister)

    graph.start("approval-4", "user-4", [{"content": "事实"}])
    result = graph.resume("approval-4", {"action": "unknown"})

    assert result["status"] == "rejected"
    assert persister.calls == []
