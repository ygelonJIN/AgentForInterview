"""界面骨架：新建对话的位置、待确认提示的层级。"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).parents[1] / "app" / "main.py"


def _run():
    return AppTest.from_file(APP_PATH, default_timeout=30).run()


def test_new_thread_button_sits_in_the_sidebar_above_the_nav():
    app = _run()

    assert not app.exception
    labels = [button.label for button in app.sidebar.button]
    assert "＋ 新建对话" in labels
    # 侧栏第一颗按钮，排在导航五项之前
    assert labels.index("＋ 新建对话") < labels.index("智能助手")

    # 对话区里不再放它（AppTest 的 app.button 会连侧栏一起返回，只能看源码）
    source = APP_PATH.read_text(encoding="utf-8")
    page = source[source.index("def render_assistant_page():"):source.index("def render_product_detail(")]
    assert "new_thread_side" not in page


def test_pending_alert_sits_in_the_sidebar_above_the_nav():
    """待确认提示跟新建对话同处品牌之下、导航之上，不再埋在会话列表下面。"""
    app = _run()
    app.session_state["show_memory_extraction"] = True
    app.session_state["candidate_memories"] = [{"content": "用户怕冷"}]
    app.run()

    assert not app.exception
    bodies = [item.proto.body for item in app.sidebar.markdown]
    alert_index = next(index for index, body in enumerate(bodies) if "项待确认" in body)
    nav_label_index = next(index for index, body in enumerate(bodies) if "工作台" in body)
    recent_index = next(index for index, body in enumerate(bodies) if "最近会话" in body)

    assert "sl-alert" in bodies[alert_index]
    assert alert_index < nav_label_index < recent_index


def test_recent_thread_rows_are_not_styled_like_nav_selection():
    """最近会话用左竖线标记，导航用整块强调底色；两者不能共用同一套选中样式。"""
    theme = (Path(__file__).parents[1] / "app" / "ui" / "theme.py").read_text(encoding="utf-8")

    recent_block = theme[theme.index('[class*="st-key-recent_thread_"]'):theme.index(".sl-user {{")]
    assert "border-left: 2px solid var(--c-accent)" in recent_block
    assert "var(--c-nav-active-bg)" not in recent_block


def test_sidebar_new_thread_button_has_its_own_emphasis_style():
    theme = (Path(__file__).parents[1] / "app" / "ui" / "theme.py").read_text(encoding="utf-8")

    block = theme[theme.index(".st-key-new_thread {{"):theme.index(".sl-brand {{")]
    assert "var(--c-accent) !important" in block
    assert "var(--radius-pill)" in block
