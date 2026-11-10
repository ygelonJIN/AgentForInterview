"""清理会话库里被"内存消息回写"重复写入的消息。

背景：追问补答由 resume 直接写库，排在 finish_turn 写的回复之后；旧版
``migrate_messages`` 只认连续前缀，错位之后每次重跑都把那一轮再写一遍，
导致同一轮在库里堆出多份。

判定：带 ``migrated_from`` 元数据、且同一线程里**更早**已存在完全相同
``(role, content)`` 的行。图自己写的消息不带这个标记，所以合法的
重复回答（模型两次给出同样的话）不会被误删。

默认只列清单；``--apply`` 才删除，删之前自动备份到
``data/conversations.db.backup-dedup-<时间戳>``（已在 ``.gitignore`` 里）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "conversations.db"


def find_duplicates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, thread_id, role, content, metadata_json
        FROM conversation_messages
        ORDER BY thread_id, id
        """
    ).fetchall()

    first_seen: dict[tuple[str, str, str], int] = {}
    duplicates: list[dict] = []
    for message_id, thread_id, role, content, metadata_json in rows:
        metadata = json.loads(metadata_json or "{}")
        key = (thread_id, role, content or "")
        if metadata.get("migrated_from") and key in first_seen:
            duplicates.append({
                "id": message_id,
                "thread_id": thread_id,
                "role": role,
                "kept_id": first_seen[key],
                "content": content or "",
            })
            continue
        first_seen.setdefault(key, message_id)
    return duplicates


def _summary(content: str, limit: int = 46) -> str:
    return content.replace("\n", " ")[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="删除会话库里重复写入的迁移消息")
    parser.add_argument("--apply", action="store_true", help="确认删除；默认只打印清单")
    parser.add_argument("--db", default=str(DB_PATH), help="会话库路径")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"没有找到会话库：{db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    try:
        duplicates = find_duplicates(conn)
        total = conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
        print(f"库：{db_path}")
        print(f"总消息 {total} 条，待删重复 {len(duplicates)} 条"
              + ("（--apply）" if args.apply else "（dry-run，不改库）"))
        for item in duplicates:
            print(f"  thread={item['thread_id']}  id={item['id']}  保留={item['kept_id']}  "
                  f"{item['role']:9s} {_summary(item['content'])}")

        if not duplicates:
            return 0
        if not args.apply:
            print("\n加 --apply 才会删。")
            return 0

        backup = db_path.with_name(
            f"{db_path.name}.backup-dedup-{time.strftime('%Y%m%d%H%M%S')}"
        )
        shutil.copy2(db_path, backup)
        print(f"\n已备份：{backup}")

        with conn:
            conn.executemany(
                "DELETE FROM conversation_messages WHERE id = ?",
                [(item["id"],) for item in duplicates],
            )
        remaining = conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
        print(f"删除完成：{total} -> {remaining}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
