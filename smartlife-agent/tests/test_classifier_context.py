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


def test_keyword_path_uses_recent_history_for_follow_up():
    result = _classifier().classify(
        "第二个呢",
        history=[{"role": "user", "content": "推荐一款手机"}],
    )

    assert result.intent == "shopping"
    assert "关键词" in result.reason


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
        "reason": "关键词匹配 (2 个关键词命中)",
        "from": "keyword",
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
