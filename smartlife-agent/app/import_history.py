"""本地导入任务审计记录。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class ImportHistory:
    def __init__(self, db_path: Optional[str | Path] = None):
        base_dir = Path(__file__).resolve().parents[1]
        self.db_path = Path(db_path) if db_path else base_dir / "data" / "import_history.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS import_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    imported INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    chunks INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_import_jobs_created "
                "ON import_jobs(created_at DESC)"
            )

    def record(
        self,
        *,
        kind: str,
        target: str,
        source: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        imported: int = 0,
        skipped: int = 0,
        deleted: int = 0,
        chunks: int = 0,
    ) -> Dict[str, Any]:
        payload = dict(result or {})
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO import_jobs (
                    kind, target, source, status, imported, skipped,
                    deleted, chunks, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    target,
                    source,
                    status,
                    int(imported),
                    int(skipped),
                    int(deleted),
                    int(chunks),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
            job_id = int(cursor.lastrowid)
        return {"id": job_id, "status": status, "result": payload}

    def list_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM import_jobs
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                item["result"] = json.loads(item.pop("result_json") or "{}")
            except json.JSONDecodeError:
                item["result"] = {}
            output.append(item)
        return output
