"""可见执行日志、重试事件和循环/分支记录测试。"""

import asyncio
from pathlib import Path

from streamlit.testing.v1 import AppTest

from app.execution_log import build_execution_log
from app.graphs.context import TurnContext
from app.graphs.subgraphs.shopping import build_shopping_graph


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
