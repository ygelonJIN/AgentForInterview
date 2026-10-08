from pathlib import Path

from streamlit.testing.v1 import AppTest

from app.memory.md_memory import MDMemory


def _open_memory_page():
    app = AppTest.from_file(Path(__file__).parents[1] / "app" / "main.py", default_timeout=20).run()
    next(button for button in app.sidebar.button if button.label.endswith("记忆中心")).click().run()
    return app


def test_memory_editor_refreshes_md_and_uses_inline_approval(monkeypatch, tmp_path):
    def fake_init(self, base_dir=None):
        self.base_dir = str(tmp_path)

    monkeypatch.setattr(MDMemory, "__init__", fake_init)
    md = MDMemory(base_dir=str(tmp_path))
    md.save_preference("user_001", "我是男生", "general")

    app = _open_memory_page()

    assert not app.exception
    preference_editor = next(
        item for item in app.text_area if item.label == "偏好记忆 Markdown"
    )
    assert "我是男生" in preference_editor.value
    assert not any(button.label == "快速添加" for button in app.button)
    assert not any(item.label == "记忆分类" for item in app.selectbox)

    next(button for button in app.button if button.label == "保存 MD 并同步向量").click().run()

    labels = [button.label for button in app.button]
    assert "同意" in labels
    assert "拒绝" in labels
    assert "批准执行" not in labels


def test_memory_center_keeps_only_summary_vector_and_md_editing(monkeypatch):
    class FakeConversationStore:
        def list_threads(self, user_id):
            return [{
                "thread_id": "user_001:shopping:session-1",
                "message_count": 2,
                "last_at": "2026-10-06 17:00",
                "preview": "找一双跑鞋",
                "user_id": "user_001",
                "scene": "shopping",
                "session_id": "session-1",
            }]

        def list_messages(self, thread_id):
            return [
                {"role": "user", "content": "找一双跑鞋"},
                {"role": "assistant", "content": "推荐轻量跑鞋"},
            ]

        def conversation_hash(self, messages):
            return "hash-1"

        def get_summary(self, thread_id, conversation_hash):
            return None

    monkeypatch.setattr(
        "app.memory.conversation_store.ConversationStore",
        FakeConversationStore,
    )
    app = _open_memory_page()

    assert not app.exception
    assert any(item.label == "选择对话" for item in app.selectbox)
    assert any(button.label == "生成摘要" for button in app.button)
    assert any(item.label == "向量记忆内容" for item in app.text_area)
    assert any(item.label == "偏好记忆 Markdown" for item in app.text_area)

    source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    memory_page = source[source.index("def render_memory_page():"):source.index("def render_model_configuration(")]
    assert "render_memory_search()" not in memory_page
    assert "render_md_memory_manager()" not in memory_page
    assert "st.json(payload)" not in source


def test_memory_center_shows_pending_candidate_memory_actions():
    app = AppTest.from_file(Path(__file__).parents[1] / "app" / "main.py", default_timeout=20).run()
    app.session_state["show_memory_extraction"] = True
    app.session_state["memory_extraction_error"] = False
    app.session_state["memory_extraction_scene"] = "auto"
    app.session_state["candidate_memories"] = [
        {"content": "用户偏好轻量跑鞋", "category": "shopping", "confidence": "high"}
    ]
    next(button for button in app.sidebar.button if button.label.endswith("记忆中心")).click().run()

    assert not app.exception
    assert any("记忆提取结果" in item.proto.body for item in app.subheader)
    labels = [button.label for button in app.button]
    assert any(label.endswith("确认写入选中") for label in labels)
    assert any(label.endswith("拒绝写入") for label in labels)
    assert any(label.endswith("全部写入") for label in labels)
