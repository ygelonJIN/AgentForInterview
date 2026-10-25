"""装备清单请求的识别与路由。"""

import asyncio

from app.agents.langgraph_orchestrator import LangGraphOrchestrator
from app.agents.orchestrator_v2 import OrchestratorV2
from app.classifier import ClassificationResult
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

    assert LangGraphOrchestrator._route(state) == "trip_gear"


def test_normal_travel_still_routes_to_travel_node():
    state = {
        "classification": {
            "intent": "travel",
            "intents": ["travel"],
            "sub_intent": "plan_trip",
            "route": "plan_and_execute",
        }
    }

    assert LangGraphOrchestrator._route(state) == "travel"


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
    orchestrator = OrchestratorV2.__new__(OrchestratorV2)
    orchestrator.classifier = _Classifier()
    orchestrator._short_term_memory = _ShortTermMemory()
    orchestrator._last_classifications = {}
    queue = _Queue()

    result = asyncio.run(
        orchestrator.classify_turn("周末去莫干山露营", "thread-1", queue)
    )

    assert result.intent == "travel"
    assert result.sub_intent == "gear_plan"
    assert result.route == "plan_and_execute"
    assert any(
        event == "classify" and data.get("sub_intent") == "gear_plan"
        for event, data in queue.events
    )


class _Agent:
    def __init__(self):
        self.calls = []

    async def run_streaming(self, message, user_id, queue, chat_history=None):
        self.calls.append((message, user_id, chat_history))
        return "ok"


class _HistoryStore:
    def get_langchain_messages(self, thread_id, last_n=10):
        return [{"role": "user", "content": "我有帐篷"}]


def test_run_trip_gear_turn_passes_previous_user_turns():
    orchestrator = OrchestratorV2.__new__(OrchestratorV2)
    orchestrator._trip_gear_agent = _Agent()
    orchestrator._short_term_memory = _HistoryStore()

    result = asyncio.run(
        orchestrator.run_trip_gear_turn("那装备呢", "user-1", "thread-1", None, None)
    )

    assert result == "ok"
    assert orchestrator._trip_gear_agent.calls[0][2] == [
        {"role": "user", "content": "我有帐篷"}
    ]
