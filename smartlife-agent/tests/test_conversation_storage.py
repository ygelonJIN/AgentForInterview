from app.memory.conversation_store import ConversationStore
from app.memory.short_term import ShortTermMemory


def test_short_term_memory_archives_raw_conversation_to_sqlite(tmp_path):
    db_path = tmp_path / "conversations.db"
    store = ConversationStore(db_path)

    memory = ShortTermMemory(conversation_store=store)
    memory.add_message("thread-1", "user", "帐篷评价怎么样？", {"scene": "shopping"})
    memory.add_message("thread-1", "assistant", "防水和通风评价都不错。")

    rows = store.list_messages("thread-1")
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "帐篷评价怎么样？"
    assert rows[0]["metadata"] == {"scene": "shopping"}
    assert store.count_messages("thread-1") == 2

    restored = ShortTermMemory(conversation_store=ConversationStore(db_path))
    history = restored.get_history("thread-1")
    assert [row["content"] for row in history] == [
        "帐篷评价怎么样？",
        "防水和通风评价都不错。",
    ]


def test_clear_removes_persistent_conversation_archive(tmp_path):
    db_path = tmp_path / "conversations.db"
    store = ConversationStore(db_path)
    memory = ShortTermMemory(conversation_store=store)
    memory.add_message("thread-1", "user", "你好")

    memory.clear("thread-1")

    assert memory.get_history("thread-1") == []
    assert store.count_messages("thread-1") == 0


def test_vector_browser_is_collapsed_by_default():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert "expanded=vector_pending" in source
    assert "vector_pending = any(" in source


def test_thread_listing_and_summary_fingerprint_prevent_duplicates(tmp_path):
    store = ConversationStore(tmp_path / "conversations.db")
    store.append_message("user-1:shopping:s-1", "user", "找一双跑鞋")
    store.append_message("user-1:shopping:s-1", "assistant", "推荐轻量跑鞋")

    threads = store.list_threads("user-1")
    assert len(threads) == 1
    assert threads[0]["scene"] == "shopping"
    assert threads[0]["message_count"] == 2
    assert "找一双跑鞋" in threads[0]["preview"]

    messages = store.list_messages("user-1:shopping:s-1")
    fingerprint = store.conversation_hash(messages)
    first = store.register_summary(
        "user-1:shopping:s-1", fingerprint, "用户需要跑鞋推荐", memory_id="m-1"
    )
    second = store.register_summary(
        "user-1:shopping:s-1", fingerprint, "重复摘要", memory_id="m-2"
    )

    assert first["memory_id"] == "m-1"
    assert second == first
    assert store.get_summary("user-1:shopping:s-1", fingerprint)["summary"] == "用户需要跑鞋推荐"


def test_legacy_in_memory_messages_migrate_idempotently(tmp_path):
    store = ConversationStore(tmp_path / "conversations.db")
    messages = [
        {"role": "user", "content": "帮我找帐篷"},
        {"role": "assistant", "response": "推荐轻量双人帐篷", "process_events": []},
        {"role": "assistant", "content": "推荐轻量双人帐篷"},
    ]

    first = store.migrate_messages("legacy-thread", messages)
    second = store.migrate_messages("legacy-thread", messages)

    assert first == {"imported": 3, "skipped": 0}
    assert second == {"imported": 0, "skipped": 3}
    rows = store.list_messages("legacy-thread")
    assert [item["role"] for item in rows] == ["user", "assistant", "assistant"]
    assert [item["content"] for item in rows[1:]] == [
        "推荐轻量双人帐篷",
        "推荐轻量双人帐篷",
    ]
    assert rows[0]["metadata"]["migrated_from"] == "in_memory"
