"""分类器多轮上下文与指代回退测试。"""

import asyncio
from types import SimpleNamespace

from app.classifier import ClassificationResult, UnifiedClassifier


class _Chain:
    def __init__(self):
        self.calls = []

    def invoke(self, values):
        self.calls.append(values)
        return SimpleNamespace(content='{"route":"react","intent":"general","sub_intent":"chat","retrieval":"none","confidence":0.7,"reason":"ctx"}')

    async def ainvoke(self, values):
        self.invoke(values)
        return SimpleNamespace(content='{"route":"react","intent":"general","sub_intent":"chat","retrieval":"none","confidence":0.7,"reason":"ctx"}')


class _Queue:
    def __init__(self):
        self.events = []

    async def emit(self, event_type, data, step=""):
        self.events.append((event_type, data, step))

    async def emit_thinking(self, message, step="", model=""):
        self.events.append(("thinking", {"message": message}, step))


def _classifier():
    classifier = UnifiedClassifier.__new__(UnifiedClassifier)
    classifier._chain = _Chain()
    return classifier


def test_history_supplies_model_context_but_does_not_count_as_keyword_evidence():
    classifier = _classifier()
    result = classifier.classify(
        "第二个呢",
        history=[{"role": "user", "content": "推荐一款手机"}],
    )

    assert result.intent == "general"
    assert classifier._chain.calls[0]["context"] == "user: 推荐一款手机"


def test_reference_follow_up_reuses_previous_classification():
    previous = ClassificationResult(
        route="react",
        intent="shopping",
        sub_intent="search",
        retrieval="mixed",
        confidence=0.75,
    )
    classifier = _classifier()

    result = classifier.classify("预算改成 800", previous=previous)

    assert result.intent == "shopping"
    assert result.route == "react"
    assert "沿用上一轮分类" in result.reason


def test_model_classification_receives_history_context():
    classifier = _classifier()
    history = [{"role": "user", "content": "我下周去杭州"}]

    asyncio.run(classifier.aclassify("那里有什么好吃的", history=history))

    assert classifier._chain.calls[0]["context"] == "user: 我下周去杭州"


def test_streaming_context_fallback_emits_context_method():
    previous = ClassificationResult(
        route="plan_and_execute",
        intent="travel",
        sub_intent="plan_trip",
        retrieval="rag_only",
        confidence=0.8,
    )
    classifier = _classifier()
    queue = _Queue()

    result = asyncio.run(classifier.aclassify_streaming(
        "就按刚才那个方案",
        queue,
        previous=previous,
    ))

    assert result.intent == "travel"
    assert queue.events[-1][1]["method"] == "context"


def test_budget_bounded_multi_day_trip_is_travel_plan_not_product_search():
    classifier = _classifier()
    queue = _Queue()

    result = asyncio.run(classifier.aclassify_streaming(
        "我要去上海，预算3000块钱玩5天",
        queue,
    ))

    assert result.intent == "travel"
    assert result.route == "plan_and_execute"
    assert result.sub_intent == "plan_trip"
    assert result.retrieval == "rag_only"
    assert queue.events[-1][1] == {
        "route": "plan_and_execute",
        "intent": "travel",
        "sub_intent": "plan_trip",
        "retrieval": "rag_only",
        "confidence": 0.85,
        "reason": "关键词匹配 (2 个关键词命中, 含弱购物词 2)",
        "from": "keyword",
        "strength": "strong",
        "method": "keyword",
    }


def test_travel_scene_overrides_product_sql_classification():
    classification = ClassificationResult(
        route="react",
        intent="shopping",
        sub_intent="search",
        retrieval="sql_only",
        confidence=0.7,
    )

    result = UnifiedClassifier.apply_scene_constraint(
        classification,
        scene="travel",
        message="我要去上海，预算3000块钱玩5天",
    )

    assert result.intent == "travel"
    assert result.route == "plan_and_execute"
    assert result.retrieval == "rag_only"
    assert "旅游场景约束" in result.reason


def test_travel_scene_preserves_explicit_cross_scene_product_request():
    classification = ClassificationResult(
        route="react",
        intent="shopping",
        sub_intent="search",
        retrieval="mixed",
        confidence=0.8,
    )

    result = UnifiedClassifier.apply_scene_constraint(
        classification,
        scene="travel",
        message="去上海玩5天，顺便买一件冲锋衣",
    )

    assert result.intent == "shopping"
    assert result.retrieval == "mixed"


def test_gear_followup_inherits_the_trip_instead_of_restarting():
    """行程中途补一句"我有一个 pocket3"是装备信息，不是新任务。

    以前它走小模型判成 general，整轮掉进通用助手：把已经排好的行程丢掉，
    反过来问用户"哪天出发、几个人、哪些城市"。
    """
    from app.classifier import ClassificationResult
    from app.graphs.nodes import route

    classifier = _classifier()
    previous = ClassificationResult(
        intent="travel", route="plan_and_execute", sub_intent="plan_trip", confidence=0.85
    )

    inherited = classifier._context_fallback("我有一个pocket3", previous)

    assert inherited is not None
    assert inherited.intent == "travel"
    assert inherited.sub_intent == "gear_plan"
    assert route({"classification": inherited.model_dump(), "user_message": "我有一个pocket3"}) == "trip_gear"


def _travel_previous():
    from app.classifier import ClassificationResult

    return ClassificationResult(
        intent="travel", route="plan_and_execute", sub_intent="plan_trip", confidence=0.85
    )


def test_followups_inherit_the_previous_agent_by_default():
    """同一段对话里，没出现新任务信号就沿用上一轮，不重新开始。"""
    classifier = _classifier()
    previous = _travel_previous()

    for message in ("明天出发", "再帮我看看预算", "酒店怎么选", "下周一"):
        inherited = classifier._context_fallback(message, previous)
        assert inherited is not None, message
        assert inherited.intent == "travel"
        assert inherited.sub_intent == "plan_trip"


def test_unrelated_questions_go_back_to_the_model():
    """和当前领域没有半点关系的提问，交给小模型判，不硬留在行程里。"""
    classifier = _classifier()

    for message in ("今天天气怎么样", "推荐一部电影"):
        assert classifier._context_fallback(message, _travel_previous()) is None, message


def test_small_talk_does_not_replan_the_trip():
    classifier = _classifier()

    for message in ("谢谢", "好的", "嗯", "ok", ""):
        assert classifier._context_fallback(message, _travel_previous()) is None, message


def test_explicit_new_task_switches_agent():
    classifier = _classifier()

    for message in ("推荐一双跑鞋", "帮我查一下订单"):
        assert classifier._context_fallback(message, _travel_previous()) is None, message


def test_streaming_classification_fills_every_prompt_variable():
    """流式分类必须把提示词里的变量都传齐。

    回归背景：`{extra_intents}` 只在同步/异步两条路径传了，流式这条漏了，
    于是凡是"关键词没命中"的消息都抛 KeyError，最后落到 confidence=0.3 的兜底，
    整轮变成 general。
    """
    from app.classifier import _CLASSIFY_PROMPT

    missing = set()

    class _StrictChain:
        async def ainvoke(self, values):
            missing.update(set(_CLASSIFY_PROMPT.input_variables) - set(values))
            return SimpleNamespace(content="{}")

    classifier = _classifier()
    classifier._chain = _StrictChain()
    classifier._arbiter_chain = _Chain()

    # 分类失败会被内部兜底吞掉，所以这里只核对"变量有没有传齐"。
    asyncio.run(classifier.aclassify_streaming("上海", _Queue()))

    assert missing == set(), f"流式分类缺少提示词变量：{missing}"
