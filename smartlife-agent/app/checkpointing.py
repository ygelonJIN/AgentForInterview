"""LangGraph checkpoint 工厂。"""
from typing import Any, Optional


def create_checkpointer(backend: str = "memory", connection: Optional[str] = None) -> Any:
    """创建 checkpointer。

    ``memory`` 用于开发和测试；``sqlite``、``postgres`` 依赖相应的独立
    langgraph checkpoint 包。业务图只依赖返回的 checkpointer 接口。
    """
    normalized = (backend or "memory").lower()
    if normalized == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver()

    if normalized == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise RuntimeError(
                "SQLite checkpointer 需要安装 langgraph-checkpoint-sqlite"
            ) from exc
        saver = SqliteSaver
        if hasattr(saver, "from_conn_string"):
            return saver.from_conn_string(connection or ":memory:")
        return saver(connection or ":memory:")

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
