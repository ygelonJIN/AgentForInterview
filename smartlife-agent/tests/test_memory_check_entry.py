"""记忆提取改成按需：一个按钮，只提取上次之后的新消息。"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from app.graphs.memory import MemoryExtractionRunner
from app.memory.conversation_store import ConversationStore
from app.memory.extractor import MemoryExtractionResult
from app.services.turn_services import TurnServices
from app.session import build_thread_id

APP_PATH = Path(__file__).parents[1] / "app" / "main.py"


class _StubServices:
    """只实现记忆提取那一个方法，图里其他能力这轮用不到。"""

    def __init__(self, result: MemoryExtractionResult):
        self.result = result
        self.calls = []

    def extract_candidate_memories_result(
        self, user_id, session_id, scene, excluded_contents=None, messages=None
    ):
        self.calls.append((user_id, session_id, scene, list(excluded_contents or []), messages))
        return self.result


def _services_with_store(tmp_path) -> tuple:
    """不跑 TurnServices.__init__：这里只需要会话库。"""
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    services = object.__new__(TurnServices)
    services._conversation_store = store
    return services, store


def test_window_takes_only_user_messages_after_cursor(tmp_path):
    services, store = _services_with_store(tmp_path)
    thread_id = build_thread_id("user_001", "general", "s1")
    store.append_message(thread_id, "user", "我怕冷")
    store.append_message(thread_id, "assistant", "收到，会给你带厚一点的")
    store.append_message(thread_id, "user", "我不吃辣")

    first = services.memory_extraction_window("user_001", "s1", "general")

    assert [item["content"] for item in first["messages"]] == ["我怕冷", "我不吃辣"]
    assert first["pending"] == 2
    assert first["cursor"] == 0
    assert first["remaining"] == 0

    services.record_memory_extraction(thread_id, first["last_message_id"])
    assert services.memory_extraction_window("user_001", "s1", "general")["messages"] == []

    store.append_message(thread_id, "user", "我住在杭州")
    third = services.memory_extraction_window("user_001", "s1", "general")

    assert [item["content"] for item in third["messages"]] == ["我住在杭州"]
    assert third["cursor"] == first["last_message_id"]


def test_window_caps_message_count_and_chars(tmp_path):
    services, store = _services_with_store(tmp_path)
    thread_id = build_thread_id("user_001", "general", "s1")
    for index in range(40):
        store.append_message(thread_id, "user", f"第 {index} 条偏好")

    window = services.memory_extraction_window("user_001", "s1", "general")

    assert len(window["messages"]) <= 20
    assert window["pending"] == 40
    assert window["remaining"] == 40 - len(window["messages"])
    assert window["chars"] <= 4000
    # 按时间顺序取，不跳着取
    assert window["messages"][0]["content"] == "第 0 条偏好"


def test_cursor_never_moves_backwards(tmp_path):
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    thread_id = build_thread_id("user_001", "general", "s1")

    store.set_extraction_cursor(thread_id, 9)
    store.set_extraction_cursor(thread_id, 4)

    assert store.get_extraction_cursor(thread_id) == 9


def test_deleting_thread_drops_its_cursor(tmp_path):
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    thread_id = build_thread_id("user_001", "general", "s1")
    store.append_message(thread_id, "user", "我怕冷")
    store.set_extraction_cursor(thread_id, 1)

    store.delete_thread(thread_id)

    assert store.get_extraction_cursor(thread_id) == 0


def test_runner_sends_the_given_window_to_the_extractor():
    services = _StubServices(MemoryExtractionResult(
        candidates=[{"content": "用户怕冷", "category": "general", "confidence": "high"}],
        status="ok",
    ))
    runner = MemoryExtractionRunner(services)
    window = [{"role": "user", "content": "我怕冷"}]

    runner.start("user_001", "session-1", "general", ["用户不吃辣"], window)
    payload = runner.wait("user_001", "session-1", "general", timeout=20)

    assert payload is not None
    assert [item["content"] for item in payload["candidates"]] == ["用户怕冷"]
    assert services.calls == [
        ("user_001", "session-1", "general", ["用户不吃辣"], window)
    ]


def test_runner_reports_error_payload_instead_of_dropping_it():
    class _BrokenServices(_StubServices):
        def extract_candidate_memories_result(self, *args, **kwargs):
            raise RuntimeError("模型超时")

    runner = MemoryExtractionRunner(_BrokenServices(MemoryExtractionResult()))
    runner.start("user_001", "session-2", "general", None, [{"role": "user", "content": "我怕冷"}])

    payload = runner.wait("user_001", "session-2", "general", timeout=20)

    assert payload is not None
    assert payload["has_error"] is True
    assert "模型超时" in payload["diagnostic"]


def test_turn_graph_exposes_window_and_cursor():
    from app.graphs.orchestrator import TurnGraph

    assert callable(getattr(TurnGraph, "memory_extraction_window", None))
    assert callable(getattr(TurnGraph, "record_memory_extraction", None))
    assert callable(getattr(TurnGraph, "start_memory_extraction", None))


def test_latest_user_message_ignores_assistant_turns():
    """这个静态方法被提取链路调用，缺了就会让每次提取都以 AttributeError 失败。"""
    history = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "我对羽绒过敏"},
    ]

    assert TurnServices._latest_user_message(history) == [{"role": "user", "content": "我对羽绒过敏"}]
    assert TurnServices._latest_user_message([]) == []
    assert TurnServices._latest_user_message([{"role": "assistant", "content": "只有回答"}]) == []


def test_answer_no_longer_triggers_memory_extraction():
    """回答结束后不该再自动跑提取：入口只有对话区那个按钮。"""
    source = APP_PATH.read_text(encoding="utf-8")

    assert "postprocess=_postprocess" not in source
    assert "_extract_memories_after_stream" not in source
    assert "start_memory_extraction" in source


class _RepoStub:
    def __init__(self):
        self.deleted = []

    def delete_memory(self, user_id, memory_id):
        self.deleted.append((user_id, memory_id))
        return True


class _ServicesStub:
    def __init__(self):
        self.calls = []
        self.memory_repository = _RepoStub()

    def save_user_selected_memories(self, user_id, candidates, selected_indices):
        self.calls.append((user_id, [item["content"] for item in candidates], selected_indices))
        return [f"memory-{index}" for index in selected_indices]


def _graph_stub():
    from app.graphs.orchestrator import TurnGraph

    graph = object.__new__(TurnGraph)
    graph.services = _ServicesStub()
    return graph


def test_single_candidate_write_goes_straight_through():
    graph = _graph_stub()
    candidate = {"content": "用户怕冷", "category": "general"}

    ids = graph.save_memory_candidates("user_001", [candidate], [0])

    assert ids == ["memory-0"]
    assert graph.services.calls == [("user_001", ["用户怕冷"], [0])]


def test_write_all_defaults_to_every_index():
    graph = _graph_stub()
    candidates = [{"content": "A"}, {"content": "B"}]

    ids = graph.save_memory_candidates("user_001", candidates)

    assert ids == ["memory-0", "memory-1"]
    assert graph.services.calls[-1][2] == [0, 1]


def test_undo_deletes_the_written_memory():
    graph = _graph_stub()

    assert graph.delete_memory("user_001", "memory-0") is True
    assert graph.services.memory_repository.deleted == [("user_001", "memory-0")]


def test_assistant_page_hides_memory_entry_before_any_conversation():
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()

    assert not app.exception
    assert not any("提取记忆" in button.label for button in app.button)


def test_assistant_page_shows_memory_entry_once_conversation_starts():
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.session_state["assistant_msgs"] = [
        {"role": "user", "content": "我怕冷"},
        {"role": "assistant", "response": "知道了", "process_events": []},
    ]
    app.run()

    assert not app.exception
    labels = [button.label for button in app.button]
    assert any(label.endswith("提取记忆") for label in labels)
    # 默认不写入：面板没出现
    assert not any("这次提取到" in item.proto.body for item in app.markdown)


def test_candidate_panel_defaults_to_unwritten_and_offers_toggle():
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.session_state["assistant_msgs"] = [{"role": "user", "content": "我怕冷"}]
    app.session_state["show_memory_extraction"] = True
    app.session_state["memory_extraction_error"] = False
    app.session_state["memory_extraction_scene"] = "general"
    app.session_state["memory_extraction_thread"] = ""
    app.session_state["memory_saved_map"] = {}
    app.session_state["candidate_memories"] = [
        {"content": "用户对羽绒过敏", "category": "general", "confidence": "high"},
        {"content": "用户常住杭州", "category": "travel", "confidence": "medium"},
    ]
    app.run()

    assert not app.exception
    bodies = [item.proto.body for item in app.markdown]
    assert any("这次提取到 2 条" in body for body in bodies)
    labels = [button.label for button in app.button]
    # 每行一个写入按钮，默认都不是已写入
    assert labels.count("写入") == 2
    assert not any(label.startswith("已写入") for label in labels)
    assert "全部写入" in labels
    assert "收起" in labels
    assert "全部忽略" not in labels
    # 不再有"确认写入选中"这类二次确认
    assert not any("确认写入选中" in label for label in labels)


def test_saved_candidate_row_offers_undo_and_all_toggle_flips():
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.session_state["assistant_msgs"] = [{"role": "user", "content": "我怕冷"}]
    app.session_state["show_memory_extraction"] = True
    app.session_state["memory_extraction_error"] = False
    app.session_state["memory_extraction_scene"] = "general"
    app.session_state["memory_extraction_thread"] = ""
    app.session_state["memory_saved_map"] = {0: "memory-0", 1: "memory-1"}
    app.session_state["candidate_memories"] = [
        {"content": "用户对羽绒过敏", "category": "general", "confidence": "high"},
        {"content": "用户常住杭州", "category": "travel", "confidence": "medium"},
    ]
    app.run()

    assert not app.exception
    labels = [button.label for button in app.button]
    assert labels.count("已写入 · 撤销") == 2
    assert "全部撤销" in labels
    assert "全部写入" not in labels


def test_candidates_stay_pending_until_handled_and_are_thread_scoped():
    """没处理完的候选不会因为继续对话而消失，但也不该串到别的会话里。"""
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.session_state["assistant_msgs"] = [
        {"role": "user", "content": "我怕冷"},
        {"role": "assistant", "response": "知道了", "process_events": []},
        {"role": "user", "content": "继续，帮我看看睡袋"},
    ]
    app.session_state["show_memory_extraction"] = True
    app.session_state["memory_extraction_error"] = False
    app.session_state["memory_extraction_scene"] = "general"
    app.session_state["memory_extraction_thread"] = ""
    app.session_state["memory_saved_map"] = {}
    app.session_state["candidate_memories"] = [
        {"content": "用户怕冷", "category": "general", "confidence": "high"}
    ]
    app.run()

    assert not app.exception
    assert any("这次提取到 1 条" in item.proto.body for item in app.markdown)

    # 换到别的会话：同一份候选不显示
    app.session_state["memory_extraction_thread"] = "someone:general:other-session"
    app.run()

    assert not app.exception
    assert not any("这次提取到" in item.proto.body for item in app.markdown)
