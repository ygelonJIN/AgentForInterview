"""通用敏感操作审批图测试。"""

import asyncio

import pytest

from app.approval_graph import ActionApprovalGraph


def test_action_approval_interrupts_then_executes_approved_action():
    calls = []

    def execute(user_id, action_type, payload, decision):
        calls.append((user_id, action_type, payload, decision))
        return {"receipt": "PAY-1"}

    graph = ActionApprovalGraph(
        execute,
        action_type="audit_write",
        approved_actions=("approve",),
    )
    pending = graph.start("approval-pay", "user-1", {"amount": 499.0})

    assert pending["status"] == "awaiting_approval"
    assert pending["interrupt"]["approval_type"] == "audit_write"
    assert pending["interrupt"]["payload"] == {"amount": 499.0}
    assert calls == []

    result = graph.resume("approval-pay", {
        "action": "approve",
        "idempotency_key": "audit-1",
    })

    assert result["status"] == "executed"
    assert result["state"]["result"] == {"receipt": "PAY-1"}
    assert calls == [(
        "user-1",
        "audit_write",
        {"amount": 499.0},
        {"action": "approve", "idempotency_key": "audit-1"},
    )]


def test_action_approval_rejects_unknown_and_reject_actions_without_execution():
    calls = []

    def execute(*args):
        calls.append(args)
        return {"receipt": "unexpected"}

    graph = ActionApprovalGraph(execute, action_type="bulk_delete", approved_actions=("approve",))
    graph.start("approval-order", "user-1", {"product_id": "p-1"})
    rejected = graph.resume("approval-order", {"action": "reject"})

    assert rejected["status"] == "rejected"
    assert rejected["state"]["result"] is None
    assert calls == []

    graph.start("approval-order-2", "user-1", {"product_id": "p-1"})
    invalid = graph.resume("approval-order-2", {"action": "unknown"})

    assert invalid["status"] == "rejected"
    assert calls == []


def test_action_approval_supports_async_executor():
    calls = []

    async def execute(user_id, action_type, payload, decision):
        calls.append((user_id, action_type, payload, decision))
        return {"receipt": "ASYNC-1"}

    graph = ActionApprovalGraph(
        execute,
        action_type="memory_delete",
        approved_actions=("approve",),
    )
    graph.start("approval-delete", "user-1", {"memory_id": "m-1"})
    result = graph.resume("approval-delete", {"action": "approve"})

    assert result["status"] == "executed"
    assert result["state"]["result"] == {"receipt": "ASYNC-1"}
    assert calls[0][1] == "memory_delete"


def test_action_approval_rejects_empty_action_type_or_actions():
    with pytest.raises(ValueError):
        ActionApprovalGraph(lambda *args: None, action_type="")
    with pytest.raises(ValueError):
        ActionApprovalGraph(lambda *args: None, action_type="audit_write", approved_actions=())
