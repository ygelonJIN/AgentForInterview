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


def test_recent_thread_rows_mark_selection_with_a_fill_not_a_bar():
    """当前会话用整行浅底 + 强调字色，不再画左侧竖条；也不套导航那种描边卡片。"""
    theme = (Path(__file__).parents[1] / "app" / "ui" / "theme.py").read_text(encoding="utf-8")

    recent_block = theme[theme.index('[class*="st-key-recent_thread_"]'):theme.index(".sl-user {{")]
    assert "border-left: 2px solid var(--c-accent)" not in recent_block
    assert "var(--c-nav-active-bg)" not in recent_block
    selected_rule = recent_block[recent_block.index('button[kind="primary"]'):]
    assert "background: var(--c-nav-hover-bg)" in selected_rule
    assert "color: var(--c-nav-active-text)" in selected_rule


def test_recent_thread_rows_carry_a_delete_control():
    """会话行要能删：每行右侧一个删除按钮，删之前出确认块。"""
    main = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    assert 'key=f"recent_delete_{index}"' in main
    assert "confirm_delete_thread" in main
    assert "delete_thread" in main

    theme = (Path(__file__).parents[1] / "app" / "ui" / "theme.py").read_text(encoding="utf-8")
    # 侧栏通用按钮规则是 (0,2,2)；删除按钮必须带侧栏前缀压过它，否则会被
    # padding 8px/12px 挤成 0 内宽，✕ 看不见。
    delete_rule = theme[theme.index(
        'section[data-testid="stSidebar"] [class*="st-key-recent_row_"] [class*="st-key-recent_delete_"] button {'
    ):]
    delete_rule = delete_rule[: delete_rule.index("}}")]
    assert "width: 20px" in delete_rule
    assert "padding: 0" in delete_rule


def test_profile_actor_line_has_no_alert_bar():
    """「当前可信 Actor」是一行说明，不再用 st.info（那条 3px 左竖条）。"""
    main = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    block = main[main.index("def render_profile_page():"):main.index("def render_import_page():")]
    assert 'st.info(f"当前可信 Actor' not in block
    assert "sl-actor" in block


def test_sidebar_new_thread_uses_the_same_button_type_as_nav_selection():
    """新建对话跟工作台选中项同一套样式：都走 type="primary"，不再单独写一套 CSS。"""
    main = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    block = main[main.index('key="new_thread_side"'):]
    block = block[: block.index("):")]
    assert 'type="primary"' in block


def test_recent_threads_use_plain_text_buttons_not_cards():
    """最近会话是纯文字行：平时 tertiary；当前那条用 primary 只为点亮 2px 左竖线，
    底色会不会变卡片由 theme 里 recent 的 primary 规则兜住。"""
    main = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    block = main[main.index('key=f"recent_thread_{index}"'):]
    block = block[: block.index("):")]
    assert 'type="primary" if is_active else "tertiary"' in block
    assert 'type="secondary"' not in block


def test_sidebar_button_styles_survive_the_tooltip_wrapper():
    """带 help 的按钮外面会多包一层 tooltip span，写 ``.stButton > button`` 就命中不了。

    侧栏的「新建对话」和每一条会话都带 help，必须用后代选择器，否则样式整段落空。
    """
    theme = (Path(__file__).parents[1] / "app" / "ui" / "theme.py").read_text(encoding="utf-8")
    css = theme[theme.index("/* ---------- 左导航 ---------- */"):theme.index(".sl-user {{")]

    assert ".stButton > button" not in css
    assert '.stButton button' in css
    for rule in ('[class*="st-key-recent_thread_"] .stButton button',
                 '.st-key-new_thread .stButton button'):
        assert rule in css


def _css_rule(theme: str, selector: str) -> str:
    start = theme.index(selector)
    end = theme.index("}}", start) + 2
    return theme[start:end]


def test_itinerary_prices_never_break_into_characters():
    """金额被挤成"¥ 3 4 0"这种竖排是不可接受的：金额一律不折行。"""
    theme = (Path(__file__).parents[1] / "app" / "ui" / "theme.py").read_text(encoding="utf-8")

    assert "white-space: nowrap" in _css_rule(theme, ".sl-tl-cost {{")
    assert "white-space: nowrap" in _css_rule(theme, ".sl-day-sum {{")
    assert "white-space: nowrap" in _css_rule(theme, ".sl-gear-item .sl-gear-price {{")
    assert "white-space: nowrap" in _css_rule(theme, ".sl-budget-num b {{")

    # 时间/活动/金额改成可换行的 flex：窄的时候金额整体换行，而不是被压成一列
    item_rule = _css_rule(theme, ".sl-tl-item {{")
    assert "flex-wrap: wrap" in item_rule
    assert "grid-template-columns" not in item_rule


def test_plan_panel_keeps_cost_markup():
    from app.ui.theme import plan_panels_html

    html = plan_panels_html({
        "itinerary": {
            "days": [{
                "day": 1,
                "title": "西湖",
                "items": [{"time": "09:00", "activity": "逛西湖", "cost": 340, "duration_min": 120}],
            }]
        },
    })

    assert 'class="sl-tl-cost"' in html
    assert "¥340" in html


def _clarify_state():
    return {
        "thread_id": "t1",
        "missing": ["人数"],
        "questions": [{
            "key": "people",
            "title": "几个人？",
            "answer_prefix": "人数",
            "options": [{"label": "1 人", "value": "1 人"}, {"label": "2 人", "value": "2 人"}],
            "allow_custom": True,
        }],
    }


def test_clarify_custom_option_reveals_a_text_input():
    """选「D 自己填」要当场出现输入框。

    以前追问卡片包在 st.form 里：表单控件在点提交前不会重跑，选了「自己填」
    也不会冒出输入框，等于这个选项点不动。
    """
    from app.main import CUSTOM_OPTION_LABEL

    app = _run()
    app.session_state["pending_clarify"] = _clarify_state()
    app.run()

    assert app.radio, "追问卡片应渲染可点选项"
    assert not [item for item in app.text_input if item.key == "clarify_custom_people"]

    app.radio[0].set_value(CUSTOM_OPTION_LABEL).run()
    assert [item for item in app.text_input if item.key == "clarify_custom_people"], \
        "选了「自己填」必须出现输入框"


def test_clarify_hides_memory_entry_and_persist_error():
    """追问期间这一屏只留卡片：记忆入口和「事件未入库」都是噪声。"""
    error = "没有找到本轮助手消息，事件未入库"
    history = [{"role": "user", "content": "帮我规划个行程"}]

    # 反证：没有追问时提示和记忆入口都在，不是测试环境里根本不渲染
    plain = _run()
    plain.session_state["assistant_msgs"] = history
    plain.session_state["turn_persist_error"] = error
    plain.run()
    assert any("本轮的步骤记录" in str(item.value) for item in plain.caption)
    assert [item for item in plain.button if item.key == "extract_memory_assistant"]

    app = _run()
    app.session_state["assistant_msgs"] = history
    app.session_state["turn_persist_error"] = error
    app.session_state["pending_clarify"] = _clarify_state()
    app.run()

    bodies = " ".join(str(item.value) for item in app.markdown)
    captions = " ".join(str(item.value) for item in app.caption)
    assert "本轮的步骤记录" not in bodies + captions
    assert "还差几个信息" in bodies
    assert not [item for item in app.button if item.key == "extract_memory_assistant"]


def test_confirm_delete_removes_the_thread_from_the_store():
    """侧栏删除走"点 ✕ → 确认块 → 真删"，确认后该 thread 的消息一条不剩。"""
    from app.memory.conversation_store import ConversationStore

    store = ConversationStore()
    thread_id = "user_001:general:delete-me"
    store.append_message(thread_id, "user", "这条会话要删掉")
    store.append_message(thread_id, "assistant", "好的")

    app = _run()
    app.session_state["pending_thread_delete"] = {"thread_id": thread_id, "title": "这条会话要删掉"}
    app.run()
    assert not app.exception
    app.sidebar.button(key="confirm_delete_thread").click().run()

    assert not app.exception
    assert store.list_messages(thread_id) == []
    assert "pending_thread_delete" not in app.session_state


def test_clarify_question_is_persisted_without_a_finalize_node():
    """追问轮被 interrupt 截住，图不写助手消息；界面得自己补，否则切回会话丢问题。"""
    import json

    from app.memory.conversation_store import ConversationStore
    from app.session import build_thread_id

    class _ClarifyOrch:
        async def process_streaming(self, message, user_id, session_id, scene="general"):
            payload = {
                "event": "clarify",
                "data": {
                    "thread_id": build_thread_id(user_id, scene, session_id),
                    "missing": ["people"],
                    "question": "还需要补充：人数。",
                    "questions": [
                        {"key": "people", "title": "几个人？", "options": [], "answer_prefix": "人数："}
                    ],
                },
            }
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    app = _run()
    app.session_state["turn_graph"] = _ClarifyOrch()
    app.chat_input[0].set_value("去莫干山露营").run()
    assert not app.exception

    thread_id = build_thread_id(
        app.session_state["user_id"], "general", app.session_state["browser_session_id"]
    )
    contents = [row["content"] for row in ConversationStore().list_messages(thread_id)]
    assert "还需要补充：人数。" in contents


def test_recent_thread_hover_detail_sits_to_the_right_of_the_sidebar():
    """会话详情的浮层要贴在侧栏右边，不能再用会在上方弹出的原生 tooltip。"""
    main = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    theme = (Path(__file__).parents[1] / "app" / "ui" / "theme.py").read_text(encoding="utf-8")

    assert "help=_thread_help" not in main
    assert 'help=f"删除会话' not in main

    tip_rule = _css_rule(theme, ".sl-thread-tip {{")
    assert "position: fixed" in tip_rule
    assert "left: calc(var(--nav-w) + 12px)" in tip_rule
    assert '[class*="st-key-recent_row_"]:hover .sl-thread-tip' in theme


def test_recent_thread_tip_renders_for_each_row():
    from app.memory.conversation_store import ConversationStore

    store = ConversationStore()
    thread_id = "user_001:general:tip-check"
    store.append_message(thread_id, "user", "这条会话要显示详情")

    app = _run()
    assert not app.exception
    tips = [item.value for item in app.sidebar.markdown if "sl-thread-tip" in item.value]
    assert tips
    assert "sl-thread-tip" in tips[0]
