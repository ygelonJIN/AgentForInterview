"""可见执行日志、重试事件和循环/分支记录测试。"""

import asyncio
from pathlib import Path

from streamlit.testing.v1 import AppTest

from app.execution_log import build_execution_log
from app.graphs.context import TurnContext
from app.graphs.subgraphs.shopping import build_shopping_graph
from app.graphs.subgraphs.travel import build_travel_plan_graph


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
                "result": {
                    "cache_status": "stale_fallback",
                    "stale": True,
                    "error": "ProviderUnavailable: route timeout",
                },
            }],
        }

    class _TravelService:
        def _build_plan_context(self, *_args, **_kwargs):
            return "上下文"

        @staticmethod
        def _remove_revision_notes(plan):
            return plan

    class _Services:
        travel_service = _TravelService()
        llm = None

        def get_chat_history(self, thread_id):
            return []

    ctx = TurnContext(services=_Services(), trace_id="trace-travel").with_extra(
        planner=planner,
        reflector=reflector,
        executor=executor,
        max_revisions=1,
    ).bind_writer(events.append)

    state = asyncio.run(build_travel_plan_graph().ainvoke(
        {"user_message": "上海2天预算500元", "user_id": "user-1", "thread_id": "trace-travel"},
        context=ctx,
    ))

    logs = [event["data"] for event in events if event["event"] == "execution_log"]
    assert state["status"] == "approved"
    assert any(
        item["kind"] == "branch_selected" and item["branch"] == "build_steps"
        for item in logs
    )
    step_iterations = [
        item for item in logs
        if item["kind"] == "loop_iteration" and item["node"] == "execute_steps"
    ]
    assert [item["iteration"] for item in step_iterations] == [1, 2]
    assert all(item["max_iterations"] == 2 for item in step_iterations)
    assert [item["details"]["revision"] for item in step_iterations] == [0, 0]
    assert any(
        item["kind"] == "loop_iteration" and item["node"] == "revise"
        and item["iteration"] == 1 and item["max_iterations"] == 1
        for item in logs
    )
    fallback_logs = [item for item in logs if item["kind"] == "tool_fallback"]
    assert fallback_logs
    assert fallback_logs[0]["details"]["cache_status"] == "stale_fallback"
    assert fallback_logs[0]["details"]["error"] == "ProviderUnavailable: route timeout"
    assert "ProviderUnavailable: route timeout" in fallback_logs[0]["message"]


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


def test_streamlit_assistant_message_displays_response_and_pipeline_timing():
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app" / "main.py",
        default_timeout=20,
    ).run()
    app.session_state["assistant_msgs"] = [{
        "role": "assistant",
        "response": "测试正文",
        "process_events": [
            {
                "event": "done",
                "data": {
                    "response": "测试正文",
                    "timing_ms": {
                        "time_to_first_token_ms": 120.0,
                        "response_ready_ms": 850.0,
                        "pipeline_ms": 850.0,
                    },
                },
            },
            {
                "event": "memory_extraction",
                "data": {
                    "status": "no_content",
                    "timing_ms": {
                        "pipeline_ms": 1234.0,
                        "post_response_ms": 384.0,
                    },
                },
            },
        ],
    }]
    app.run()

    rendered = "\n".join(item.value for item in app.markdown)
    assert not app.exception
    assert "链路耗时" in rendered
    assert "首 Token 120.0ms" in rendered
    assert "回答完成 850.0ms" in rendered
    assert "回答后处理 384.0ms" in rendered
    assert "完整链路 1234.0ms" in rendered


def test_shopping_graph_logs_retrieval_degradation_and_single_repair():
    class Legacy:
        async def plan_shopping_retrieval(self, _message, _classification, _queue):
            return {"needs_product": True, "needs_review": True}, "mixed"

        async def retrieve_shopping_context(self, _message, _strategy, _needs, _queue, user_id=None):
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

    collected = []
    ctx = TurnContext(services=Legacy(), trace_id="trace-shopping").bind_writer(collected.append)

    result = asyncio.run(build_shopping_graph().ainvoke(
        {
            "user_message": "推荐跑鞋",
            "user_id": "user-1",
            "thread_id": "user-1:shopping:test",
            "classification": {"intent": "shopping", "route": "react"},
        },
        context=ctx,
    ))
    response = result["response"]
    logs = [event["data"] for event in collected if event["event"] == "execution_log"]

    assert response == "推荐真实跑鞋"
    degradation = next(item for item in logs if item["kind"] == "degradation")
    assert degradation["details"]["source_status"] == {"sql": "error", "rag": "ok"}
    assert "sql: 小模型生成 SQL 失败" in degradation["message"]
    assert any(item["kind"] == "validation_failed" for item in logs)
    assert any(
        item["kind"] == "repair_succeeded" and item["iteration"] == 1
        for item in logs
    )
