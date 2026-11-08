"""原始会话消息的关系型归档。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote


class ConversationStore:
    """把完整对话存入 SQLite，与长期偏好/事件向量记忆分离。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else self.default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def default_db_path() -> Path:
        """默认落在 ``data/conversations.db``；测试用 ``SMARTLIFE_CONVERSATIONS_DB`` 换库。

        AppTest 跑 ``app/main.py`` 时会把 session_state 里的消息迁移进库，
        不隔离就会把测试夹具写进真实会话历史里。
        """
        override = str(os.environ.get("SMARTLIFE_CONVERSATIONS_DB") or "").strip()
        if override:
            return Path(override)
        return Path(__file__).resolve().parents[2] / "data" / "conversations.db"

    def _initialize(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_thread
                ON conversation_messages(thread_id, id);
                CREATE INDEX IF NOT EXISTS idx_conversation_created
                ON conversation_messages(created_at);

                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    thread_id TEXT NOT NULL,
                    conversation_hash TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    memory_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (thread_id, conversation_hash)
                );

                CREATE TABLE IF NOT EXISTS memory_extraction_cursor (
                    thread_id TEXT PRIMARY KEY,
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def split_thread_id(thread_id: str) -> Dict[str, str]:
        parts = str(thread_id).split(":", 2)
        if len(parts) != 3:
            return {"user_id": "", "scene": "", "session_id": thread_id}
        return {
            "user_id": unquote(parts[0]),
            "scene": unquote(parts[1]),
            "session_id": unquote(parts[2]),
        }

    @staticmethod
    def message_fingerprint(role: str, content: str) -> str:
        payload = f"{role}:{content}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def migrate_messages(
        self,
        thread_id: str,
        messages: List[Dict[str, Any]],
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """幂等迁移旧的内存消息；已存在的 role/content 对不会重复写入。"""
        migration_source = (
            (metadata or {}).get("legacy_session_key")
            or (metadata or {}).get("migration_source")
            or "in_memory"
        )
        normalized = []
        for item_index, item in enumerate(messages or []):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = item.get("content")
            if content is None:
                content = item.get("response", "")
            content = str(content or "").strip()
            if role not in {"user", "assistant", "system"} or not content:
                continue
            item_metadata = dict(metadata or {})
            item_metadata.update(item.get("metadata") or {})
            item_metadata.setdefault("migrated_from", "in_memory")
            item_metadata["migration_source"] = str(migration_source)
            item_metadata["migration_index"] = int(
                item.get("migration_index", item_index)
            )
            normalized.append((
                role,
                content,
                item_metadata,
                item.get("created_at") or item.get("timestamp"),
            ))

        existing = self.list_messages(thread_id)
        fingerprints = [
            self.message_fingerprint(item["role"], item["content"])
            for item in existing
        ]
        normalized_fingerprints = [
            self.message_fingerprint(role, content)
            for role, content, _metadata, _created_at in normalized
        ]

        # 已有完整、连续的同序列消息时全部跳过；否则只跳过可证明已经导入的前缀。
        # 这样既不会在重跑时重复写入，也不会吞掉合法的完全重复消息。
        max_prefix = 0
        for start in range(len(fingerprints)):
            length = 0
            while (
                length < len(normalized_fingerprints)
                and start + length < len(fingerprints)
                and normalized_fingerprints[length] == fingerprints[start + length]
            ):
                length += 1
            max_prefix = max(max_prefix, length)

        imported = 0
        skipped = 0
        for index, (role, content, item_metadata, created_at) in enumerate(normalized):
            if index < max_prefix:
                skipped += 1
                continue
            self.append_message(
                thread_id,
                role,
                content,
                metadata=item_metadata,
                created_at=created_at,
            )
            imported += 1
        return {"imported": imported, "skipped": skipped}

    @staticmethod
    def conversation_hash(messages: List[Dict[str, Any]]) -> str:
        payload = "\n".join(
            f"{item.get('role', '')}:{item.get('content', '')}"
            for item in messages
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def list_threads(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT thread_id,
                       COUNT(*) AS message_count,
                       MAX(created_at) AS last_at,
                       MAX(CASE WHEN role = 'user' THEN content END) AS last_user_message
                FROM conversation_messages
                GROUP BY thread_id
                ORDER BY MAX(id) DESC
                """
            ).fetchall()

        output = []
        for row in rows:
            identity = self.split_thread_id(row["thread_id"])
            if user_id and identity["user_id"] != user_id:
                continue
            preview = str(row["last_user_message"] or "").strip().replace("\n", " ")
            output.append({
                "thread_id": row["thread_id"],
                "message_count": int(row["message_count"]),
                "last_at": row["last_at"],
                "preview": preview[:60],
                **identity,
            })
        return output

    def get_summary(
        self,
        thread_id: str,
        conversation_hash: str,
    ) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT thread_id, conversation_hash, summary, memory_id, created_at
                FROM conversation_summaries
                WHERE thread_id = ? AND conversation_hash = ?
                """,
                (thread_id, conversation_hash),
            ).fetchone()
        return dict(row) if row else None

    def register_summary(
        self,
        thread_id: str,
        conversation_hash: str,
        summary: str,
        memory_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO conversation_summaries
                    (thread_id, conversation_hash, summary, memory_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    conversation_hash,
                    summary,
                    memory_id,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
        return self.get_summary(thread_id, conversation_hash)

    @staticmethod
    def _metadata(metadata: Optional[Dict[str, Any]]) -> str:
        return json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)

    def append_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        created_at: Optional[str] = None,
    ) -> int:
        if not thread_id:
            raise ValueError("thread_id 不能为空")
        if role not in {"user", "assistant", "system"}:
            raise ValueError("role 必须是 user、assistant 或 system")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO conversation_messages
                    (thread_id, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    role,
                    content,
                    self._metadata(metadata),
                    created_at or datetime.now().isoformat(),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def annotate_last_assistant(self, thread_id: str, metadata: Dict[str, Any]) -> bool:
        """给该 thread 最后一条助手消息补元数据（例如本轮的流式事件）。

        历史回看要还原结构化面板，就必须把 ``process_events`` 一起存下来；
        写入时机在 ``finish_turn`` 之后，所以这里用 UPDATE 而不是再插一条。
        """
        if not thread_id or not metadata:
            return False
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, metadata_json FROM conversation_messages
                WHERE thread_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            if row is None:
                return False
            merged = {}
            try:
                merged = json.loads(row[1] or "{}")
            except json.JSONDecodeError:
                merged = {}
            merged.update(metadata)
            conn.execute(
                "UPDATE conversation_messages SET metadata_json = ? WHERE id = ?",
                (self._metadata(merged), row[0]),
            )
            conn.commit()
        return True

    def list_messages(self, thread_id: str, last_n: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = """
            SELECT id, thread_id, role, content, metadata_json, created_at
            FROM conversation_messages
            WHERE thread_id = ?
            ORDER BY id ASC
        """
        params: List[Any] = [thread_id]
        if last_n:
            sql = """
                SELECT * FROM (
                    SELECT id, thread_id, role, content, metadata_json, created_at
                    FROM conversation_messages
                    WHERE thread_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
            """
            params.append(int(last_n))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()

        output = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                item["metadata"] = {}
            output.append(item)
        return output

    def count_messages(self, thread_id: Optional[str] = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            if thread_id:
                row = conn.execute(
                    "SELECT COUNT(*) FROM conversation_messages WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()
        return int(row[0])

    def delete_thread(self, thread_id: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM conversation_messages WHERE thread_id = ?",
                (thread_id,),
            )
            conn.execute(
                "DELETE FROM memory_extraction_cursor WHERE thread_id = ?",
                (thread_id,),
            )
            conn.commit()
            return int(cursor.rowcount)

    # ---------- 记忆提取游标 ----------
    def get_extraction_cursor(self, thread_id: str) -> int:
        """上次把哪些消息送去提取过；0 表示这个会话还没提取过。"""
        if not thread_id:
            return 0
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT last_message_id FROM memory_extraction_cursor WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def set_extraction_cursor(self, thread_id: str, last_message_id: int) -> None:
        """只前进不后退：并发或重复点击都不能把游标拨回去重跑一遍。"""
        if not thread_id or int(last_message_id or 0) <= 0:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_extraction_cursor (thread_id, last_message_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    last_message_id = MAX(last_message_id, excluded.last_message_id),
                    updated_at = excluded.updated_at
                """,
                (thread_id, int(last_message_id), datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
