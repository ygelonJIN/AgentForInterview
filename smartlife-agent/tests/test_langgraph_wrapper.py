"""LangGraph 顶层条件路由和事件保持测试。"""

import asyncio
import json
import threading
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

    async def _scene(self, name, user_message, user_id, thread_id, classification, queue, **kwargs):
        self.scene_calls.append((name, user_message, user_id, thread_id))
        return f"{name}-ok"

    async def run_shopping_turn(self, *args, **kwargs):
        return await self._scene("shopping", *args, **kwargs)

    async def run_travel_turn(self, *args, **kwargs):
        return await self._scene("travel", *args, **kwargs)

    async def run_customer_service_turn(self, *args, **kwargs):
        return await self._scene("customer_service", *args, **kwargs)

    async def run_general_plan_turn(self, *args, **kwargs):
        return await self._scene("general_plan", *args, **kwargs)

    async def synthesize_composite_response(self, user_message, shopping, travel, constraints, queue):
        return "【购物推荐】\n" + shopping.get("response", "") + "\n\n【旅行计划】\n" + travel.get("response", "")

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
        ("customer_service", "react"): "customer_service",
        ("travel", "plan_and_execute"): "travel",
        ("general", "react"): "react",
        ("general", "plan_and_execute"): "general_plan",
    }
    for (intent, route), expected in cases.items():
        legacy = _FakeLegacy(intent=intent, route=route)
        wrapper = LangGraphOrchestrator(legacy)

        events = _events(_collect(wrapper))

        assert [event["event"] for event in _business_events(events)] == ["step", "done"]
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

    assert {name for name, *_ in legacy.scene_calls} == {"shopping", "travel"}
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


def test_graph_runs_composite_scenes_in_parallel():
    both_started = asyncio.Event()
    started = set()

    class ParallelLegacy(_FakeLegacy):
        async def _scene(self, name, user_message, user_id, thread_id, classification, queue, **kwargs):
            self.scene_calls.append((name, user_message, user_id, thread_id))
            started.add(name)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)
            return f"{name}-ok"

    legacy = ParallelLegacy(intent="travel", route="plan_and_execute", intents=["shopping", "travel"])
    wrapper = LangGraphOrchestrator(legacy)

    events = _events(_collect(
        wrapper,
        message="给我推荐一双800元的跑鞋，再给我一份杭州玩两天的计划",
    ))

    assert started == {"shopping", "travel"}
    done = next(event for event in events if event["event"] == "done")
    assert "【购物推荐】" in done["data"]["response"]
    assert "【旅行计划】" in done["data"]["response"]


def test_composite_sections_stream_before_done():
    class StreamingLegacy(_FakeLegacy):
        async def run_shopping_turn(self, *args, **kwargs):
            queue = args[-1]
            await queue.emit_token("真实跑鞋", step="generate")
            return "真实跑鞋"

        async def run_travel_turn(self, *args, **kwargs):
            queue = args[-1]
            await queue.emit_token("杭州两日游", step="plan")
            return "杭州两日游"

    legacy = StreamingLegacy(
        intent="travel",
        route="plan_and_execute",
        intents=["shopping", "travel"],
    )
    wrapper = LangGraphOrchestrator(legacy)

    events = _events(_collect(
        wrapper,
        message="推荐跑鞋并规划杭州两日游",
    ))
    event_types = [event["event"] for event in events]
    tokens = [event for event in events if event["event"] == "token"]

    assert event_types.index("token") < event_types.index("done")
    assert {(token["data"]["token"], token["data"]["section"]) for token in tokens} == {
        ("真实跑鞋", "shopping"),
        ("杭州两日游", "travel"),
    }


def test_memory_extraction_runs_after_response_without_blocking_stream():
    started = threading.Event()
    release = threading.Event()

    class BackgroundMemoryLegacy(_FakeLegacy):
        def extract_candidate_memories_result(
            self, user_id, session_id, scene, excluded_contents=None
        ):
            started.set()
            release.wait(1)
            return SimpleNamespace(
                as_dict=lambda: {
                    "candidates": [{"content": "喜欢跑步", "category": "shopping"}],
                    "diagnostic": "",
                    "status": "ok",
                    "has_error": False,
                    "should_display": True,
                }
            )

    legacy = BackgroundMemoryLegacy()
    wrapper = LangGraphOrchestrator(legacy)

    events = _events(_collect(wrapper))

    assert _business_events(events)[-1]["event"] == "done"
    assert wrapper.is_memory_extraction_pending("user-1", "browser-1", "shopping")
    assert started.wait(0.5)
    release.set()
    result = wrapper.wait_for_memory_extraction(
        "user-1", "browser-1", "shopping", timeout=1
    )
    assert result["candidates"][0]["content"] == "喜欢跑步"


def test_graph_state_does_not_store_runtime_event_queue():
    legacy = _FakeLegacy()
    wrapper = LangGraphOrchestrator(legacy)

    _collect(wrapper)

    state_fields = set(wrapper.graph.get_graph().nodes)
    assert "queue" not in wrapper.graph.builder.schemas.get("state", {})
    assert {"prepare", "classify", "shopping", "travel", "customer_service", "general_plan", "react", "general", "finalize"} <= state_fields


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
    assert _business_events(events)[-1]["event"] == "done"
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
