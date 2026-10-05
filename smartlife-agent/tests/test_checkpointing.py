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
