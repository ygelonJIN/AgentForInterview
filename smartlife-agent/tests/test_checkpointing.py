"""checkpoint 工厂测试。"""

import pytest

from app.checkpointing import create_checkpointer


def test_memory_checkpointer_is_created_by_default():
    checkpointer = create_checkpointer()
    assert checkpointer is not None
    assert create_checkpointer("memory") is not None


def test_unknown_checkpointer_backend_is_rejected():
    with pytest.raises(ValueError, match="不支持"):
        create_checkpointer("unknown")


def test_postgres_checkpointer_requires_connection():
    with pytest.raises((RuntimeError, ValueError)):
        create_checkpointer("postgres")


def test_sqlite_checkpointer_persists_across_instances(tmp_path):
    from langgraph.checkpoint.base import empty_checkpoint

    path = tmp_path / "checkpoints.db"
    first = create_checkpointer("sqlite", str(path))
    config = first.put(
        {"configurable": {"thread_id": "persist-1"}},
        empty_checkpoint(),
        {"source": "test", "step": 1},
        {"channel": 1},
    )
    second = create_checkpointer("sqlite", str(path))
    restored = second.get_tuple({"configurable": {"thread_id": "persist-1"}})

    assert restored is not None
    assert restored.config["configurable"]["checkpoint_id"] == config["configurable"]["checkpoint_id"]
    assert restored.metadata["source"] == "test"
