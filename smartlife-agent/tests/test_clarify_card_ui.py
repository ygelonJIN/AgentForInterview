"""追问卡片的多选：只有答案是集合的题给多选，其余保持单选。

用 AppTest 直接跑 app/main.py：卡片是界面逻辑，光看 build_questions 的
``multi`` 字段证明不了"界面上真的能多选"。
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).parents[1] / "app" / "main.py"


def _run_with_questions(questions):
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.session_state["assistant_msgs"] = []
    app.session_state["pending_clarify"] = {"thread_id": "t1", "missing": [], "questions": questions}
    app.run()
    return app


def _city_question():
    return {
        "key": "cities",
        "title": "广东去哪几个城市？",
        "answer_prefix": "目的地",
        "options": [{"label": "广州", "value": "广州"},
                    {"label": "深圳", "value": "深圳"},
                    {"label": "珠海", "value": "珠海"}],
        "allow_custom": True,
        "multi": True,
        "hint": "可多选：按选中的顺序串联",
    }


def _days_question():
    return {
        "key": "days",
        "title": "玩几天？",
        "answer_prefix": "天数",
        "options": [{"label": f"{days} 天", "value": f"{days} 天"} for days in (2, 3, 5)],
        "allow_custom": True,
        "multi": False,
        "hint": "",
    }


def test_city_question_renders_as_multi_select_pills():
    app = _run_with_questions([_city_question()])

    assert not app.exception
    assert len(app.pills) == 1
    assert app.pills[0].options == ["广州", "深圳", "珠海"]
    # 多选的题不该再出现单选圆点
    assert len(app.radio) == 0


def test_days_question_stays_single_choice():
    """天数多选本身就是自相矛盾的答案，界面上不能给多选控件。"""
    app = _run_with_questions([_days_question()])

    assert not app.exception
    assert len(app.pills) == 0
    assert len(app.radio) == 1


def test_multiple_pills_can_be_selected_at_once():
    app = _run_with_questions([_city_question()])

    app.pills[0].set_value(["广州", "珠海"]).run()

    assert not app.exception
    assert app.pills[0].value == ["广州", "珠海"]


def test_city_and_days_together_render_one_of_each():
    app = _run_with_questions([_city_question(), _days_question()])

    assert not app.exception
    assert len(app.pills) == 1 and len(app.radio) == 1
