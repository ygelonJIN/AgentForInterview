"""装备清单请求的识别与路由。"""

import asyncio

from app.classifier import ClassificationResult
from app.graphs.context import TurnContext
from app.graphs.nodes import route
from app.graphs.subgraphs.trip_gear import build_trip_gear_graph, history_text
from app.services.turn_services import TurnServices
from app.trip_gear.intent import is_gear_request


def test_gear_requests_are_detected():
    assert is_gear_request("准备去露营三天，帮我列一份装备清单")
    assert is_gear_request("周末两个人去莫干山露营，预算1500")
    assert is_gear_request("出差三天带什么")


def test_scene_requests_without_gear_word_are_detected():
    assert is_gear_request("下周三去上海出差两天，见客户")
    assert is_gear_request("三亚度假四天，带小孩")


def test_buying_requests_stay_in_shopping():
    assert not is_gear_request("我想买个帐篷，预算600")
    assert not is_gear_request("推荐露营装备")
    assert not is_gear_request("去露营要买什么")


def test_plain_travel_is_not_hijacked():
    assert not is_gear_request("去杭州玩两天")
    assert not is_gear_request("三亚五天大概多少钱")


def test_gear_plan_routes_to_trip_gear_node():
    state = {
        "classification": {
            "intent": "travel",
            "intents": ["travel"],
            "sub_intent": "gear_plan",
            "route": "plan_and_execute",
        }
    }

    assert route(state) == "trip_gear"


def test_trip_plan_routes_to_travel_without_gear_section():
    """纯旅行规划走 travel：同一张图、同一套面板，只是没有装备段。"""
    state = {
        "classification": {
            "intent": "travel",
            "intents": ["travel"],
            "sub_intent": "plan_trip",
            "route": "plan_and_execute",
        },
        "user_message": "去莫干山玩三天",
    }

    assert route(state) == "travel"


def test_gear_scene_words_pull_trip_plan_into_trip_gear():
    """说到露营/装备就是"行程 + 装备协同"，不能只排行程。"""
    state = {
        "classification": {
            "intent": "travel",
            "intents": ["travel"],
            "sub_intent": "plan_trip",
            "route": "plan_and_execute",
        },
        "user_message": "周末去莫干山露营三天",
    }

    assert route(state) == "trip_gear"


def test_travel_question_still_routes_to_travel_node():
    state = {
        "classification": {
            "intent": "travel",
            "intents": ["travel"],
            "sub_intent": "qa",
            "route": "react",
        }
    }

    assert route(state) == "travel"


class _Classifier:
    async def aclassify_streaming(self, *_args, **_kwargs):
        return ClassificationResult(intent="general", route="react", sub_intent="chat")


class _Queue:
    def __init__(self):
        self.events = []

    async def emit(self, event_type, data, step=""):
        self.events.append((getattr(event_type, "value", event_type), data))


class _ShortTermMemory:
    def add_message(self, *_args, **_kwargs):
        return None

    def get_history(self, *_args, **_kwargs):
        return []


def test_classify_turn_marks_gear_request_as_travel_gear_plan():
    services = TurnServices.__new__(TurnServices)
    services.classifier = _Classifier()
    services._short_term_memory = _ShortTermMemory()
    services._last_classifications = {}
    queue = _Queue()

    result = asyncio.run(
        services.classify("周末去莫干山露营", "thread-1", queue)
    )

    assert result.intent == "travel"
    assert result.sub_intent == "gear_plan"
    assert result.route == "plan_and_execute"
    assert any(
        event == "classify" and data.get("sub_intent") == "gear_plan"
        for event, data in queue.events
    )


class _HistoryStore:
    def get_langchain_messages(self, thread_id, last_n=10):
        return [{"role": "user", "content": "周末一个人去莫干山露营两天，2026-11-10 出发，我有帐篷和睡袋"}]


def test_trip_gear_subgraph_inherits_previous_user_turns():
    """上一轮用户说过的话必须进入装备子图，助手建议不能算作用户已有。"""
    history = _HistoryStore().get_langchain_messages("thread-1")

    assert history_text(history) == "周末一个人去莫干山露营两天，2026-11-10 出发，我有帐篷和睡袋"

    class _Services:
        def get_chat_history(self, thread_id):
            return history

    async def planner(facts, constraints):
        from app.trip_gear.itinerary import fallback_itinerary

        return fallback_itinerary(facts)

    ctx = TurnContext(services=_Services(), trace_id="gear").with_extra(
        planner=planner,
        product_search=lambda query="", limit=5: [],
    )
    result = asyncio.run(build_trip_gear_graph().ainvoke(
        {"user_message": "那装备清单呢", "user_id": "user-1", "thread_id": "thread-1"},
        context=ctx,
    ))

    assert result["status"] == "ok"
    assert "已有（上文）" in result["response"]
