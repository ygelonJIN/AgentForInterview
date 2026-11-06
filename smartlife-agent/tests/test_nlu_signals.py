"""意图打分、路由、装备识别与检索需求。"""

import pytest

from app.classifier import ClassificationResult, UnifiedClassifier, _keyword_classify
from app.nlu.signals import decide_route, score_intents
from app.services.turn_services import TurnServices
from app.trip_gear.intent import is_gear_request


@pytest.mark.parametrize(
    "message,ready",
    [
        ("推荐一双跑鞋", True),
        ("预算3000以内的手机", True),
        ("价格多少钱", False),  # 只有钱数词，没有品类
        ("便宜点", False),
        ("你好", False),
    ],
)
def test_shopping_requires_a_real_product_noun(message, ready):
    assert score_intents(message).shopping_ready is ready


def test_product_terms_are_not_double_counted():
    signal = score_intents("跑步鞋")
    assert signal.product_hits == ["跑步鞋"]


def test_negated_product_is_ignored():
    signal = score_intents("不要帐篷，我想买睡袋")
    assert "帐篷" not in signal.product_hits
    assert "睡袋" in signal.product_hits


@pytest.mark.parametrize(
    "message,expected",
    [
        ("推荐一双跑鞋", "react"),
        ("帮我做一个杭州两天行程", "plan_and_execute"),
        ("我要去上海，预算3000块钱玩5天", "plan_and_execute"),
        ("规划一下露营装备", "plan_and_execute"),
    ],
)
def test_route(message, expected):
    assert decide_route(message) == expected


@pytest.mark.parametrize(
    "message,expected",
    [
        # 以前这里只判成 shopping，丢了旅游那一半
        ("买一个帐篷和睡袋，再去莫干山露营两天", "composite"),
        ("推荐跑鞋，再制定杭州两天计划", "composite"),
        ("推荐一双跑鞋", "shopping"),
        ("去莫干山玩三天", "travel"),
        ("退一下订单", "customer_service"),
    ],
)
def test_keyword_route_targets(message, expected):
    from app.graphs.nodes import route

    result = _keyword_classify(message)
    assert result is not None, message
    assert route({"classification": result}) == expected


def test_keyword_layer_returns_none_when_evidence_is_weak():
    assert _keyword_classify("你好") is None
    assert _keyword_classify("价格多少钱") is None


@pytest.mark.parametrize(
    "message,expected",
    [
        ("准备去露营三天，帮我列一份装备清单", True),
        ("周末两个人去莫干山露营，预算1500", True),
        ("出差三天带什么", True),
        ("三亚度假四天，带小孩", True),
        ("帮我列一个露营装备清单", True),
        # 有购买动作就走购物/复合，不进装备链路
        ("我想买个帐篷，预算600", False),
        ("推荐露营装备", False),
        ("去露营要买什么", False),
        # 普通旅行不劫持
        ("去杭州玩两天", False),
        ("三亚五天大概多少钱", False),
    ],
)
def test_is_gear_request(message, expected):
    assert is_gear_request(message) is expected


def _services():
    return TurnServices.__new__(TurnServices)


def test_retrieval_needs_use_classifier_prior_when_keywords_are_blind():
    classification = ClassificationResult(
        intent="shopping", retrieval="mixed", route="react", sub_intent="search"
    )
    needs = _services()._classify_data_needs("有没有轻一点的", classification)
    assert needs["needs_product"] is True
    assert needs["needs_review"] is True
    assert needs["strategy"] == "mixed"


def test_retrieval_needs_prefer_keyword_evidence_over_prior():
    classification = ClassificationResult(
        intent="shopping", retrieval="sql_only", route="react", sub_intent="search"
    )
    needs = _services()._classify_data_needs("这双鞋耐用吗", classification)
    assert needs["needs_product"] is False
    assert needs["needs_review"] is True
    assert needs["strategy"] == "rag_only"


def test_model_failure_falls_back_to_keyword_evidence():
    result = UnifiedClassifier._fallback("model down", "推荐一双跑鞋")

    assert result.intent == "shopping"
    assert result.route == "react"
    assert result.confidence == 0.4
    assert "关键词兜底" in result.reason


def test_model_failure_without_evidence_stays_general():
    result = UnifiedClassifier._fallback("model down", "帮我处理一下")

    assert result.intent == "general"
    assert result.confidence == 0.3


class _GeneralClassifier:
    async def aclassify_streaming(self, *_args, **_kwargs):
        return ClassificationResult(intent="general", route="react", sub_intent="chat")


class _Queue:
    def __init__(self):
        self.events = []

    async def emit(self, event_type, data, step=""):
        self.events.append((getattr(event_type, "value", event_type), data))


class _Memory:
    def add_message(self, *_args, **_kwargs):
        return None

    def get_history(self, *_args, **_kwargs):
        return []


def test_money_only_message_is_not_forced_into_shopping():
    """旧安全网把"预算/元/块"当产品词，一句"预算3000"会被判成购物。"""
    import asyncio

    services = TurnServices.__new__(TurnServices)
    services.classifier = _GeneralClassifier()
    services._short_term_memory = _Memory()
    services._last_classifications = {}

    result = asyncio.run(services.classify("预算3000", "thread-1", _Queue()))

    assert result.intent == "general"
