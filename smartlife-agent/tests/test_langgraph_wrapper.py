"""LangGraph 顶层条件路由和事件保持测试。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.langgraph_orchestrator import LangGraphOrchestrator
from app.streaming import EventType


def _events(sse_lines):
    return [json.loads(line.removeprefix("data: ")) for line in sse_lines]


class _FakeLegacy:
    def __init__(self, intent="shopping", route="react", error=None):
        self.intent = intent
        self.route = route
        self.error = error
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
            model_dump=lambda: {"intent": self.intent, "route": self.route},
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

        assert [event["event"] for event in events] == ["step", "done", "memory_extraction"]
        assert legacy.scene_calls[0][0] == expected
        done = next(event for event in events if event["event"] == "done")
        assert done["data"]["response"] == f"{expected}-ok"


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


def test_graph_wrapper_turns_execution_failure_into_error_event():
    legacy = _FakeLegacy(error=RuntimeError("execution failed"))
    wrapper = LangGraphOrchestrator(legacy)

    events = _events(_collect(wrapper))

    assert events[-1]["event"] == "error"
    assert "execution failed" in events[-1]["data"]["message"]
