"""LangGraph checkpoint 工厂。"""
import os
from typing import Any, Optional


def create_checkpointer(backend: Optional[str] = None, connection: Optional[str] = None) -> Any:
    """创建 checkpointer。

    ``memory`` 用于开发和测试；``sqlite``、``postgres`` 依赖相应的独立
    langgraph checkpoint 包。业务图只依赖返回的 checkpointer 接口。
    """
    normalized = (backend or os.environ.get("SMARTLIFE_CHECKPOINTER_BACKEND") or "sqlite").lower()
    if normalized == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver()

    if normalized == "sqlite":
        from app.checkpoint_sqlite import SQLiteCheckpointSaver
        return SQLiteCheckpointSaver(
            connection
            or os.environ.get("SMARTLIFE_CHECKPOINTER_SQLITE_PATH")
            or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data",
                "checkpoints.db",
            )
        )

    if normalized in {"postgres", "postgresql"}:
        try:
            from langgraph.checkpoint.postgres import AsyncPostgresSaver
        except ImportError as exc:
            raise RuntimeError(
                "Postgres checkpointer 需要安装 langgraph-checkpoint-postgres"
            ) from exc
        if not connection:
            raise ValueError("Postgres checkpointer 需要连接串")
        return AsyncPostgresSaver.from_conn_string(connection)

    raise ValueError(f"不支持的 checkpointer backend: {backend}")
