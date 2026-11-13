"""运行过程面板：分类证据、检索选择、正在干什么都要显示出来。

这里不调模型：直接把一轮真实形状的事件塞进助手消息，看渲染出来的过程卡片
有没有把"选了哪条检索 / 命中了哪些词 / 刚才在干什么"写清楚。
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).parents[1] / "app" / "main.py"


def _app_with_events(events):
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.session_state["assistant_msgs"] = [
        {"role": "user", "content": "推荐登山鞋，去杭州玩三天要带什么"},
        {"role": "assistant", "response": "", "process_events": events},
    ]
    app.run()
    return app


def _bodies(app):
    return "\n".join(str(item.proto.body) for item in app.markdown)


def _classify_events():
    return [
        {
            "event": "step",
            "step": "classify",
            "data": {
                "step": 1,
                "total": 4,
                "name": "统一分类",
                "description": "关键词直通（强证据） · 意图 购物 · 检索 混合（商品属性 + 评价/攻略） · 关键词命中 3 个",
                "status": "completed",
            },
        },
        {
            "event": "classify",
            "step": "classify",
            "data": {
                "route": "react",
                "intent": "shopping",
                "sub_intent": "search",
                "retrieval": "mixed",
                "confidence": 0.85,
                "reason": "关键词匹配 (2 个关键词命中)",
                "method": "keyword",
                "strength": "strong",
                "keyword_hits": {"shopping": ["登山鞋", "推荐"], "travel": ["杭州"]},
                "keyword_scores": {"shopping": 2.6, "travel": 0.8},
            },
        },
    ]


def test_classify_card_shows_retrieval_choice_and_keyword_hits():
    app = _app_with_events(_classify_events())

    assert not app.exception
    body = _bodies(app)
    # 选了哪条检索，以及它意味着查什么
    assert "检索 混合 · 商品属性 + 评价/攻略" in body
    # 内部枚举换成人话，同时保留原值方便对照代码
    assert "购物(shopping)" in body
    assert "判定 关键词直通（强证据）" in body
    # 命中的关键词按意图摊开
    assert "命中关键词" in body
    assert "购物 登山鞋 / 推荐" in body
    assert "旅行 杭州" in body
    assert "关键词得分" in body and "购物 2.6" in body


def test_step_card_repeats_the_same_retrieval_decision():
    app = _app_with_events(_classify_events())

    body = _bodies(app)
    assert "统一分类" in body
    assert "关键词命中 3 个" in body


def test_rag_tool_card_shows_query_hits_and_latency():
    app = _app_with_events([
        {
            "event": "tool_call",
            "step": "retrieval",
            "data": {
                "tool": "rag",
                "input": "查询「杭州 三天 攻略」",
                "status": "completed",
                "output": "命中 2 条评价/攻略",
                "docs": 2,
                "elapsed_ms": 320.0,
            },
        },
    ])

    assert not app.exception
    body = _bodies(app)
    assert "评价与攻略检索（RAG）" in body
    assert "查询「杭州 三天 攻略」" in body
    assert "命中 2 条评价/攻略 · 320ms" in body


def test_empty_keyword_layer_says_so_instead_of_staying_silent():
    app = _app_with_events([
        {
            "event": "classify",
            "step": "classify",
            "data": {
                "intent": "general",
                "route": "react",
                "retrieval": "none",
                "confidence": 0.7,
                "reason": "闲聊",
                "method": "llm",
                "strength": "none",
                "keyword_hits": {},
                "keyword_scores": {"shopping": 0.0, "travel": 0.0},
            },
        },
    ])

    assert not app.exception
    body = _bodies(app)
    assert "未命中 · 交小模型" in body
    assert "无（这句没有任何词表命中）" in body
    assert "判定 小模型判定" in body


def test_finished_turn_never_shows_a_step_still_running():
    """收尾后的过程记录里不该再有"进行中"。

    阶段在中断处停下（追问、等预算比例）时，它开始那条 active 事件会留在事件流里；
    用户看到"跑完了 01 还写着进行中"，只会以为卡住了。
    """
    app = _app_with_events([
        {
            "event": "step",
            "step": "derive",
            "data": {
                "step": 1, "total": 8, "name": "约束派生",
                "description": "正在从这句话里抽目的地", "status": "active",
            },
        },
        {
            "event": "step",
            "step": "plan",
            "data": {
                "step": 3, "total": 8, "name": "逐日行程",
                "description": "已生成 5 天行程", "status": "completed",
            },
        },
    ])

    assert not app.exception
    body = _bodies(app)
    assert "进行中" not in body
    assert body.count("已完成") >= 2


def test_waiting_stage_is_shown_as_waiting_for_you():
    app = _app_with_events([
        {
            "event": "step",
            "step": "clarify",
            "data": {
                "step": 1, "total": 8, "name": "约束派生",
                "description": "等你补充：出发地、交通、住宿（补齐就接着排）",
                "status": "waiting",
            },
        },
    ])

    assert not app.exception
    body = _bodies(app)
    # 状态签必须是"待确认"（warn 色），不能是"进行中"或"已完成"
    assert 'sl-tag--warn sl-tag--pill">待确认' in body
    assert "等你补充" in body
    assert "进行中" not in body
    assert 'sl-tag--ok sl-tag--pill">已完成' not in body


def test_process_panel_opens_while_waiting_and_collapses_after_finishing():
    """等用户回答时最近一轮展开；答完（不再等）就收起。

    追问停下时若折成「运行过程 · 8 条」，用户看到的就是"步骤全没了"；
    全部答完还摊着一大块过程，又压着正文。
    """
    older = {
        "role": "assistant",
        "response": "更早的一轮",
        "process_events": [{
            "event": "step",
            "data": {"step": 1, "total": 8, "name": "约束派生",
                     "description": "已派生 3 条约束", "status": "completed"},
        }],
    }
    latest = {
        "role": "assistant",
        "response": "还需要补充：出发地",
        "process_events": [{
            "event": "step",
            "data": {"step": 1, "total": 8, "name": "约束派生",
                     "description": "等你补充：出发地", "status": "waiting"},
        }],
    }
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    app.session_state["assistant_msgs"] = [
        {"role": "user", "content": "第一句"},
        older,
        {"role": "user", "content": "第二句"},
        latest,
    ]
    # 正在等用户回答：最近一轮展开，更早的折起来
    app.session_state["pending_clarify"] = {
        "thread_id": "t", "missing": ["出发地"],
        "questions": [{
            "key": "origin", "title": "从哪个城市出发？", "answer_prefix": "出发地",
            "options": [{"label": "上海", "value": "上海"}], "allow_custom": True,
            "multi": False, "hint": "",
        }],
    }
    app.run()

    assert not app.exception
    body = _bodies(app)
    assert "等你补充：出发地" in body
    assert body.count("<details") == 1
    assert "运行过程 · 1 条" in body

    # 答完了（没有待回答的追问）：最近一轮也收起
    app.session_state["pending_clarify"] = {}
    app.run()
    body = _bodies(app)
    assert body.count("<details") == 2


def test_live_action_is_not_repeated_when_the_card_already_says_it():
    """卡片描述已经是"正在生成…"时，下面不再挂一行一模一样的话。"""
    app = _app_with_events([
        {
            "event": "step",
            "step": "plan",
            "data": {
                "step": 2, "total": 4, "name": "逐日行程",
                "description": "🧠 正在生成 5 天逐日行程（广州）",
                "status": "active",
            },
        },
        {
            "event": "thinking",
            "step": "plan",
            "data": {"message": "🧠 正在生成 5 天逐日行程（广州）"},
        },
    ])

    assert not app.exception
    body = _bodies(app)
    assert body.count("正在生成 5 天逐日行程（广州）") == 1


def test_keyword_badge_distinguishes_no_hit_from_not_enough_evidence():
    app = _app_with_events([
        {
            "event": "classify",
            "step": "classify",
            "data": {
                "intent": "travel", "route": "plan_and_execute", "retrieval": "rag_only",
                "confidence": 0.95, "method": "llm", "strength": "none",
                "keyword_hits": {"travel": ["<时长>"]},
                "keyword_scores": {"shopping": 0.0, "travel": 1.0},
                "reason": "多步行程规划",
            },
        },
    ])

    assert not app.exception
    body = _bodies(app)
    # 有命中但没到 1.2：不能写"未命中"
    assert "有命中但不够定案 · 交小模型" in body
    assert "（定案线 1.2）" in body
