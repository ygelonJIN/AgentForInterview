"""LangGraph 顶层条件路由和事件保持测试。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.langgraph_orchestrator import LangGraphOrchestrator
from app.streaming import EventType


def _events(sse_lines):
    return [json.loads(line.removeprefix("data: ")) for line in sse_lines]


def _business_events(events):
    return [event for event in events if event["event"] != "execution_log"]


class _FakeLegacy:
    def __init__(self, intent="shopping", route="react", error=None, intents=None):
        self.intent = intent
        self.route = route
        self.error = error
        self.intents = list(intents or [])
        self.begin_calls = []
        self.scene_calls = []
        self.finish_calls = []

    async def begin_turn(self, user_message, thread_id):
        self.begin_calls.append((user_message, thread_id))

    async def classify_turn(self, user_message, thread_id, queue):
        if self.error:
            raise self.error
        return SimpleNamespace(
            intent=self.intent,
            route=self.route,
            model_dump=lambda: {
                "intent": self.intent,
                "intents": self.intents,
                "route": self.route,
            },
        )

    async def _scene(self, name, user_message, user_id, thread_id, classification, queue):
        self.scene_calls.append((name, user_message, user_id, thread_id))
        return f"{name}-ok"

    async def run_shopping_turn(self, *args):
        return await self._scene("shopping", *args)

    async def run_travel_turn(self, *args):
        return await self._scene("travel", *args)

    async def run_negotiation_turn(self, *args):
        return await self._scene("negotiation", *args)

    async def run_react_turn(self, *args):
        return await self._scene("react", *args)

    async def run_general_turn(self, *args):
        return await self._scene("general", *args)

    def extract_candidate_memories_result(self, user_id, session_id, scene, excluded_contents=None):
        return SimpleNamespace(
            as_dict=lambda: {
                "candidates": [],
                "diagnostic": "",
                "status": "no_content",
                "has_error": False,
                "should_display": False,
            }
        )

    async def finish_turn(self, thread_id, response, classification, queue):
        self.finish_calls.append((thread_id, response))
        await queue.emit(EventType.STEP, {"name": "wrapped"}, step="graph")
        await queue.emit(EventType.DONE, {"response": response}, step="done")

    def delegated_method(self):
        return "delegated"


def _collect(wrapper, scene="shopping", message="hello"):
    async def run():
        return [line async for line in wrapper.process_streaming(
            message, "user-1", "browser-1", scene=scene
        )]

    return asyncio.run(run())


def test_travel_scene_never_routes_budget_trip_to_product_agent():
    legacy = _FakeLegacy(intent="shopping", route="react")
    wrapper = LangGraphOrchestrator(legacy)

    events = _events(_collect(
        wrapper,
        scene="travel",
        message="我要去上海，预算3000块钱玩5天",
    ))

    assert legacy.scene_calls[0][0] == "travel"
    done = next(event for event in events if event["event"] == "done")
    assert done["data"]["response"] == "travel-ok"


def test_graph_routes_each_intent_to_its_scene_node():
    cases = {
        ("shopping", "react"): "shopping",
        ("customer_service", "react"): "shopping",
        ("travel", "plan_and_execute"): "travel",
        ("negotiation", "react"): "negotiation",
        ("general", "react"): "react",
        ("general", "plan_and_execute"): "general",
    }
    for (intent, route), expected in cases.items():
        legacy = _FakeLegacy(intent=intent, route=route)
        wrapper = LangGraphOrchestrator(legacy)

        events = _events(_collect(wrapper))

        assert [event["event"] for event in _business_events(events)] == [
            "step", "done", "memory_extraction"
        ]
        assert legacy.scene_calls[0][0] == expected
        branches = [
            event["data"]["branch"]
            for event in events
            if event["event"] == "execution_log"
            and event["data"]["kind"] == "branch_selected"
        ]
        assert expected in branches
        done = next(event for event in events if event["event"] == "done")
        assert done["data"]["response"] == f"{expected}-ok"


def test_graph_runs_both_shopping_and_travel_for_explicit_composite_intent():
    legacy = _FakeLegacy(
        intent="travel",
        route="plan_and_execute",
        intents=["shopping", "travel"],
    )
    wrapper = LangGraphOrchestrator(legacy)

    events = _events(_collect(
        wrapper,
        message="给我推荐一双800元的跑鞋，再给我一份杭州玩两天的计划",
    ))

    assert [name for name, *_ in legacy.scene_calls] == ["shopping", "travel"]
    branches = [
        event["data"]["branch"]
        for event in events
        if event["event"] == "execution_log"
        and event["data"]["kind"] == "branch_selected"
    ]
    assert "shopping_travel" in branches
    done = next(event for event in events if event["event"] == "done")
    assert "【购物推荐】" in done["data"]["response"]
    assert "【旅行计划】" in done["data"]["response"]


def test_graph_state_does_not_store_runtime_event_queue():
    legacy = _FakeLegacy()
    wrapper = LangGraphOrchestrator(legacy)

    _collect(wrapper)

    state_fields = set(wrapper.graph.get_graph().nodes)
    assert "queue" not in wrapper.graph.builder.schemas.get("state", {})
    assert {"prepare", "classify", "shopping", "travel", "negotiation", "react", "general", "finalize"} <= state_fields


def test_graph_wrapper_preserves_thread_scope_and_delegation():
    legacy = _FakeLegacy()
    wrapper = LangGraphOrchestrator(legacy)

    _collect(wrapper)

    assert legacy.begin_calls == [("hello", "user-1:shopping:browser-1")]
    assert legacy.finish_calls == [("user-1:shopping:browser-1", "shopping-ok")]
    assert wrapper.delegated_method() == "delegated"


def test_top_level_shopping_budget_covers_nested_generation_repair():
    policies = LangGraphOrchestrator._default_node_policies()

    assert policies["shopping"].timeout_seconds >= 300
    assert policies["shopping"].retry_policy.max_attempts == 1


def test_graph_wrapper_turns_execution_failure_into_error_event():
    legacy = _FakeLegacy(error=RuntimeError("execution failed"))
    wrapper = LangGraphOrchestrator(legacy)

    events = _events(_collect(wrapper))

    assert events[-1]["event"] == "error"
    assert "execution failed" in events[-1]["data"]["message"]


def test_graph_node_policy_retries_transient_classification_failures():
    from app.reliability import NodePolicy, RetryableError, RetryPolicy

    class RetryOnce(_FakeLegacy):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def classify_turn(self, user_message, thread_id, queue):
            self.calls += 1
            if self.calls == 1:
                raise RetryableError("temporary classifier outage")
            return await super().classify_turn(user_message, thread_id, queue)

    legacy = RetryOnce()
    wrapper = LangGraphOrchestrator(
        legacy,
        node_policies={
            **LangGraphOrchestrator._default_node_policies(),
            "classify": NodePolicy(
                timeout_seconds=2,
                retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
                retryable_exceptions=(RetryableError,),
            ),
        },
    )

    events = _events(_collect(wrapper))

    assert legacy.calls == 2
    assert _business_events(events)[-1]["event"] in {"done", "memory_extraction"}
    retry_logs = [
        event["data"] for event in events
        if event["event"] == "execution_log"
        and event["data"]["node"] == "classify"
        and event["data"]["kind"] in {"attempt_failed", "retry_scheduled", "node_succeeded"}
    ]
    assert any(item["kind"] == "attempt_failed" for item in retry_logs)
    assert any(item["kind"] == "retry_scheduled" for item in retry_logs)
    assert any(
        item["kind"] == "node_succeeded" and item["attempt"] == 2
        for item in retry_logs
    )
