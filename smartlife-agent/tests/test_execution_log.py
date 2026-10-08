"""可见执行日志、重试事件和循环/分支记录测试。"""

import asyncio
import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from app.agents.shopping_graph import ShoppingGraph
from app.agents.travel_graph import TravelPlanGraph
from app.execution_log import build_execution_log, run_policy_node
from app.reliability import NodePolicy, RetryableError, RetryPolicy
from app.streaming import EventQueue


def _sse_events(queue):
    async def collect():
        await queue.finish()
        return [
            json.loads(line.removeprefix("data: "))
            async for line in queue.to_sse()
        ]

    return asyncio.run(collect())


def test_policy_run_emits_limits_attempts_and_retry_schedule():
    queue = EventQueue(trace_id="trace-policy")
    policy = NodePolicy(
        timeout_seconds=2,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
        retryable_exceptions=(RetryableError,),
    )
    calls = []

    async def operation(attempt):
        calls.append(attempt)
        if attempt == 1:
            raise RetryableError("temporary")
        return "ok"

    result = asyncio.run(run_policy_node(
        policy,
        operation,
        queue=queue,
        graph="test",
        node="classify",
        step="classify",
        branch="shopping",
    ))

    events = _sse_events(queue)
    logs = [event["data"] for event in events if event["event"] == "execution_log"]
    assert result == "ok"
    assert calls == [1, 2]
    assert logs[0]["kind"] == "node_started"
    assert logs[0]["details"]["retry_max_attempts"] == 2
    assert logs[0]["details"]["timeout_seconds"] == 2
    assert any(
        item["kind"] == "attempt_failed" and item["details"]["will_retry"] is True
        for item in logs
    )
    assert any(
        item["kind"] == "retry_scheduled" and item["details"]["next_attempt"] == 2
        for item in logs
    )
    assert logs[-1]["kind"] == "node_succeeded"
    assert logs[-1]["attempt"] == 2
    assert logs[-1]["branch"] == "shopping"


def test_travel_graph_logs_branch_revision_and_step_iterations():
    events = []

    async def planner(request, context, previous_plan=None, feedback=None, on_token=None):
        return "第1天：A\n第2天：B"

    reflector_results = [
        {"is_satisfactory": False, "suggestions": ["修复"]},
        {"is_satisfactory": True, "suggestions": []},
    ]

    async def reflector(_request, _plan):
        return reflector_results.pop(0)

    async def executor(step, _state):
        return {
            "status": "completed",
            "result": f"done:{step['id']}",
            "tool_calls": [{
                "tool": "get_weather",
                "ok": True,
                "result": {"cache_status": "stale_fallback", "stale": True},
            }],
        }

    graph = TravelPlanGraph(
        planner=planner,
        reflector=reflector,
        executor=executor,
        on_event=lambda event, data: events.append((event, data)),
        max_revisions=1,
    )

    state = asyncio.run(graph.run_streaming(
        "上海2天预算500元",
        "user-1",
        "",
        thread_id="trace-travel",
    ))

    logs = [data for event, data in events if event == "execution_log"]
    assert state["status"] == "approved"
    assert any(
        item["kind"] == "branch_selected" and item["branch"] == "make_plan"
        for item in logs
    )
    step_iterations = [
        item for item in logs
        if item["kind"] == "loop_iteration" and item["node"] == "execute_steps"
    ]
    assert [item["iteration"] for item in step_iterations] == [1, 2, 1, 2]
    assert all(item["max_iterations"] == 2 for item in step_iterations)
    assert [item["details"]["revision"] for item in step_iterations] == [0, 0, 1, 1]
    assert any(
        item["kind"] == "loop_iteration" and item["node"] == "revise"
        and item["iteration"] == 1 and item["max_iterations"] == 1
        for item in logs
    )
    fallback_logs = [item for item in logs if item["kind"] == "tool_fallback"]
    assert fallback_logs
    assert fallback_logs[0]["details"]["cache_status"] == "stale_fallback"


def test_execution_log_redacts_sensitive_details():
    payload = build_execution_log(
        graph="test",
        node="node",
        kind="node_failed",
        message="failed",
        status="error",
        details={"api_key": "secret", "thread_id": "thread-1"},
    )

    assert payload["data"]["details"]["api_key"] == "[redacted]"
    assert payload["data"]["details"]["thread_id"] == "thread-1"


def test_streamlit_assistant_message_displays_execution_log_panel():
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app" / "main.py",
        default_timeout=20,
    ).run()
    app.session_state["assistant_msgs"] = [{
        "role": "assistant",
        "response": "测试正文",
        "process_events": [{
            "event": "execution_log",
            "data": {
                "log_id": "log-1",
                "graph": "shopping",
                "node": "generate",
                "kind": "node_failed",
                "status": "error",
                "message": "显示日志测试",
                "attempt": 1,
                "max_attempts": 1,
                "iteration": None,
                "max_iterations": None,
                "branch": None,
                "duration_ms": 12.3,
                "details": {"error_type": "ConnectionError"},
            },
            "step": "generate",
        }],
    }]
    app.run()

    rendered = "\n".join(item.value for item in app.markdown)
    assert not app.exception
    assert "执行日志" in rendered
    assert "显示日志测试" in rendered
    assert "shopping:generate" in rendered


def test_shopping_graph_logs_retrieval_degradation_and_single_repair():
    class Legacy:
        async def plan_shopping_retrieval(self, _message, _classification, _queue):
            return {"needs_product": True, "needs_review": True}, "mixed"

        async def retrieve_shopping_context(self, _message, _strategy, _needs, _queue):
            return {
                "source_status": {"sql": "error", "rag": "ok"},
                "source_errors": {"sql": "OpenAIConnectionError: Connection error."},
                "sql_failure_stage": "nl2sql_model",
                "partial": True,
                "products": [{"name": "真实跑鞋"}],
            }

        async def generate_shopping_response(
            self,
            *_args,
            validation_feedback="",
            **_kwargs,
        ):
            return "推荐真实跑鞋" if validation_feedback else "推荐不存在的商品"

        def validate_shopping_response(self, response, _retrieval):
            if "真实跑鞋" not in response:
                raise ValueError("必须引用真实商品")

    queue = EventQueue(trace_id="trace-shopping")
    graph = ShoppingGraph(Legacy())

    response = asyncio.run(graph.run_streaming(
        "推荐跑鞋",
        "user-1",
        "user-1:shopping:test",
        {"intent": "shopping", "route": "react"},
        queue,
    ))
    events = _sse_events(queue)
    logs = [event["data"] for event in events if event["event"] == "execution_log"]

    assert response == "推荐真实跑鞋"
    degradation = next(item for item in logs if item["kind"] == "degradation")
    assert degradation["details"]["source_status"] == {"sql": "error", "rag": "ok"}
    assert "sql: 小模型生成 SQL 失败" in degradation["message"]
    assert any(item["kind"] == "validation_failed" for item in logs)
    assert any(
        item["kind"] == "repair_succeeded" and item["iteration"] == 1
        for item in logs
    )
