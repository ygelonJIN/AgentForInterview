"""在线 Rerank A/B 曝光、点击和转化记录。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


class ExperimentLogger:
    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        salt: Optional[str] = None,
        experiment: str = "rerank-v1",
    ):
        base_dir = Path(__file__).resolve().parents[2]
        self.db_path = Path(db_path) if db_path else base_dir / "data" / "evaluation" / "ab_events.db"
        self.salt = salt or os.environ.get("SMARTLIFE_AB_SALT", "smartlife-local-ab")
        self.experiment = experiment
        self._initialize()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ranking_events (
                    event_id TEXT PRIMARY KEY,
                    experiment TEXT NOT NULL,
                    subject_hash TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    rank INTEGER,
                    document_id TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ranking_events_query "
                "ON ranking_events(experiment, query_hash)"
            )
            connection.commit()

    @staticmethod
    def _hash(value: str, salt: str) -> str:
        return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()

    def assign_variant(self, user_id: str, query: str) -> str:
        digest = self._hash(f"{self.experiment}:{user_id}:{query}", self.salt)
        return "control" if int(digest[:8], 16) % 2 == 0 else "treatment"

    def _insert(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str,
        variant: str,
        event_type: str,
        rank: Optional[int],
        document_id: Optional[str],
        metadata: Mapping[str, Any],
    ) -> str:
        if variant not in {"control", "treatment"}:
            raise ValueError("variant 必须是 control 或 treatment")
        if event_type not in {"impression", "click", "conversion", "rejection"}:
            raise ValueError("不支持的 event_type")
        if rank is not None and rank < 0:
            raise ValueError("rank 不能为负数")
        event_id = uuid.uuid4().hex
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO ranking_events (
                    event_id, experiment, subject_hash, session_hash, query_hash,
                    query_text, variant, event_type, rank, document_id,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    self.experiment,
                    self._hash(user_id, self.salt),
                    self._hash(session_id, self.salt),
                    self._hash(query, self.salt),
                    query if os.environ.get("SMARTLIFE_AB_STORE_QUERY_TEXT", "").lower() in {"1", "true", "yes", "on"} else "",
                    variant,
                    event_type,
                    rank,
                    document_id,
                    json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        return event_id

    def log_impression(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str,
        variant: Optional[str] = None,
        ranked_document_ids: Optional[List[str]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        if not query.strip():
            raise ValueError("query 不能为空")
        selected = variant or self.assign_variant(user_id, query)
        return self._insert(
            user_id=user_id,
            session_id=session_id,
            query=query,
            variant=selected,
            event_type="impression",
            rank=None,
            document_id=None,
            metadata={
                **dict(metadata or {}),
                "ranked_document_ids": list(ranked_document_ids or []),
            },
        )

    def log_outcome(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str,
        event_type: str,
        rank: int,
        document_id: str,
        variant: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        selected = variant or self.assign_variant(user_id, query)
        return self._insert(
            user_id=user_id,
            session_id=session_id,
            query=query,
            variant=selected,
            event_type=event_type,
            rank=rank,
            document_id=document_id,
            metadata=metadata or {},
        )

    def summary(self) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT variant, event_type, COUNT(*)
                FROM ranking_events
                WHERE experiment = ?
                GROUP BY variant, event_type
                """,
                (self.experiment,),
            ).fetchall()
        variants: Dict[str, Dict[str, Any]] = {
            "control": {"impressions": 0, "clicks": 0, "conversions": 0, "rejections": 0},
            "treatment": {"impressions": 0, "clicks": 0, "conversions": 0, "rejections": 0},
        }
        for variant, event_type, count in rows:
            variants.setdefault(variant, dict.fromkeys(
                ("impressions", "clicks", "conversions", "rejections"), 0
            ))[f"{event_type}s"] = count
        for values in variants.values():
            impressions = values["impressions"] or 0
            values["click_through_rate"] = values["clicks"] / impressions if impressions else 0.0
            values["conversion_rate"] = values["conversions"] / impressions if impressions else 0.0
        return {"experiment": self.experiment, "variants": variants}
