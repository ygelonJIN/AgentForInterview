"""项目内置的 SQLite LangGraph Checkpointer。"""
from __future__ import annotations

import random
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    ChannelVersions,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langchain_core.runnables import RunnableConfig


class SQLiteCheckpointSaver(BaseCheckpointSaver[str]):
    """把完整 checkpoint、channel values 和 pending writes 持久化到 SQLite。"""

    def __init__(self, connection: str | Path = "checkpoints.db"):
        super().__init__()
        self.connection = str(connection)
        if self.connection != ":memory:":
            Path(self.connection).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.connection, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=OFF")
        return connection

    def _init_db(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    checkpoint_type TEXT NOT NULL,
                    checkpoint_blob BLOB NOT NULL,
                    metadata_type TEXT NOT NULL,
                    metadata_blob BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                );
                CREATE TABLE IF NOT EXISTS checkpoint_values (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_blob BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, channel),
                    FOREIGN KEY (thread_id, checkpoint_ns, checkpoint_id)
                        REFERENCES checkpoints(thread_id, checkpoint_ns, checkpoint_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS checkpoint_writes (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    write_idx INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_blob BLOB NOT NULL,
                    task_path TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx),
                    FOREIGN KEY (thread_id, checkpoint_ns, checkpoint_id)
                        REFERENCES checkpoints(thread_id, checkpoint_ns, checkpoint_id)
                        ON DELETE CASCADE
                );
                """
            )

    @staticmethod
    def _config_key(config: RunnableConfig) -> tuple[str, str, str]:
        configurable = config.get("configurable") or {}
        thread_id = str(configurable.get("thread_id") or "")
        if not thread_id:
            raise ValueError("checkpoint config 缺少 thread_id")
        checkpoint_ns = str(configurable.get("checkpoint_ns") or "")
        checkpoint_id = get_checkpoint_id(config) or ""
        return thread_id, checkpoint_ns, checkpoint_id

    def _load_tuple(self, row: sqlite3.Row, connection: sqlite3.Connection) -> CheckpointTuple:
        thread_id = row["thread_id"]
        checkpoint_ns = row["checkpoint_ns"]
        checkpoint_id = row["checkpoint_id"]
        checkpoint = self.serde.loads_typed((row["checkpoint_type"], row["checkpoint_blob"]))
        values = {
            item["channel"]: self.serde.loads_typed((item["value_type"], item["value_blob"]))
            for item in connection.execute(
                """
                SELECT channel, value_type, value_blob
                FROM checkpoint_values
                WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?
                """,
                (thread_id, checkpoint_ns, checkpoint_id),
            )
        }
        checkpoint["channel_values"] = values
        metadata = self.serde.loads_typed((row["metadata_type"], row["metadata_blob"]))
        writes = [
            (item["task_id"], item["channel"], self.serde.loads_typed((item["value_type"], item["value_blob"])))
            for item in connection.execute(
                """
                SELECT task_id, channel, value_type, value_blob
                FROM checkpoint_writes
                WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?
                ORDER BY task_id, write_idx
                """,
                (thread_id, checkpoint_ns, checkpoint_id),
            )
        ]
        parent_id = row["parent_checkpoint_id"]
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id else None
            ),
            pending_writes=writes,
        )

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id, checkpoint_ns, checkpoint_id = self._config_key(config)
        with self._lock, self._connect() as connection:
            if checkpoint_id:
                row = connection.execute(
                    """
                    SELECT * FROM checkpoints
                    WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?
                    """,
                    (thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM checkpoints
                    WHERE thread_id=? AND checkpoint_ns=?
                    ORDER BY checkpoint_id DESC LIMIT 1
                    """,
                    (thread_id, checkpoint_ns),
                ).fetchone()
            return self._load_tuple(row, connection) if row else None

    def list(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[Dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        clauses = []
        params: list[Any] = []
        if config:
            thread_id, checkpoint_ns, _ = self._config_key(config)
            clauses.extend(["thread_id=?", "checkpoint_ns=?"])
            params.extend((thread_id, checkpoint_ns))
        if before:
            before_id = get_checkpoint_id(before)
            if before_id:
                clauses.append("checkpoint_id < ?")
                params.append(before_id)
        sql = "SELECT * FROM checkpoints"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY checkpoint_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            for row in rows:
                item = self._load_tuple(row, connection)
                if filter and any(item.metadata.get(key) != value for key, value in filter.items()):
                    continue
                yield item

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns, _ = self._config_key(config)
        parent_id = (config.get("configurable") or {}).get("checkpoint_id")
        payload = dict(checkpoint)
        values = dict(payload.pop("channel_values") or {})
        checkpoint_type, checkpoint_blob = self.serde.dumps_typed(payload)
        metadata_type, metadata_blob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        checkpoint_id = checkpoint["id"]
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO checkpoints(
                    thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                    checkpoint_type, checkpoint_blob, metadata_type, metadata_blob
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id, checkpoint_ns, checkpoint_id, parent_id,
                    checkpoint_type, checkpoint_blob, metadata_type, metadata_blob,
                ),
            )
            for channel, value in values.items():
                value_type, value_blob = self.serde.dumps_typed(value)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO checkpoint_values(
                        thread_id, checkpoint_ns, checkpoint_id, channel, value_type, value_blob
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (thread_id, checkpoint_ns, checkpoint_id, channel, value_type, value_blob),
                )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, checkpoint_ns, checkpoint_id = self._config_key(config)
        if not checkpoint_id:
            raise ValueError("put_writes 缺少 checkpoint_id")
        with self._lock, self._connect() as connection:
            for index, (channel, value) in enumerate(writes):
                write_idx = WRITES_IDX_MAP.get(channel, index)
                value_type, value_blob = self.serde.dumps_typed(value)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO checkpoint_writes(
                        thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx,
                        channel, value_type, value_blob, task_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx,
                        channel, value_type, value_blob, task_path,
                    ),
                )

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        return self.get_tuple(config)

    async def alist(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[Dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ):
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)

    def delete_thread(self, thread_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))

    def get_next_version(self, current: Optional[str], channel: Any) -> str:
        if current is None:
            current_value = 0
        elif isinstance(current, int):
            current_value = current
        else:
            current_value = int(str(current).split(".", 1)[0])
        return f"{current_value + 1:032}.{random.random():016}"
