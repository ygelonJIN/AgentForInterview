"""统一异常日志和敏感上下文脱敏测试。"""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app.observability import (
    current_trace_id,
    get_trace_recorder,
    extract_model_usage,
    log_exception,
    log_warning,
    timed_span,
    trace_context,
)
from app.graphs.events import EventSink, TurnTiming, to_sse


def test_log_exception_records_component_and_redacts_secrets(caplog):
    try:
        raise RuntimeError("boom")
    except RuntimeError as error:
        with caplog.at_level(logging.ERROR, logger="smartlife"):
            log_exception("test.component", error, {
                "thread_id": "thread-1",
                "api_key": "sk-secret-value",
            })

    text = caplog.text
    assert "test.component" in text
    assert "RuntimeError" in text
    assert "thread-1" in text
    assert "sk-secret-value" not in text
    assert "api_key" in text
    assert "[redacted]" in text


def test_log_warning_keeps_safe_context(caplog):
    with caplog.at_level(logging.WARNING, logger="smartlife"):
        log_warning("test.warning", "recoverable", {"detail": "ok"})

    assert "test.warning" in caplog.text
    assert "recoverable" in caplog.text


def test_timed_span_records_success_and_error():
    recorder = get_trace_recorder()
    recorder.clear()

    with timed_span("unit.success", trace_id="trace-1", attributes={"step": "ok"}):
        pass
    with pytest.raises(RuntimeError):
        with timed_span("unit.error", trace_id="trace-1"):
            raise RuntimeError("boom")

    summary = recorder.summary()
    assert summary["count"] == 2
    assert summary["errors"] == 1
    assert summary["spans"]["unit.success"]["count"] == 1
    assert summary["spans"]["unit.error"]["count"] == 1
    assert "p50_ms" in summary["spans"]["unit.success"]
    assert "p95_ms" in summary["spans"]["unit.success"]
    assert "p99_ms" in summary["spans"]["unit.success"]


def test_event_sink_carries_stable_trace_id_in_sse():
    chunks = []
    sink = EventSink(chunks.append, TurnTiming(), trace_id="trace-fixed")
    asyncio.run(sink.emit("step", {"name": "test"}))

    payloads = [json.loads(to_sse(chunk).removeprefix("data: ")) for chunk in chunks]

    assert payloads
    assert all(item["trace_id"] == "trace-fixed" for item in payloads)


def test_model_usage_is_recorded_with_optional_cost(monkeypatch):
    recorder = get_trace_recorder()
    recorder.clear()
    response = SimpleNamespace(usage_metadata={
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    })

    with timed_span("model.usage", trace_id="trace-usage") as measurement:
        measurement.add_attributes(extract_model_usage(response))

    summary = recorder.summary()["spans"]["model.usage"]
    assert summary["prompt_tokens"] == 100
    assert summary["completion_tokens"] == 50
    assert summary["total_tokens"] == 150


def test_turn_usage_sums_only_this_trace_and_ignores_other_turns():
    recorder = get_trace_recorder()
    recorder.clear()
    first = SimpleNamespace(usage_metadata={
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    })
    second = SimpleNamespace(usage_metadata={
        "input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 30,
    })
    other = SimpleNamespace(usage_metadata={
        "input_tokens": 999,
        "output_tokens": 999,
        "total_tokens": 1998,
    })

    with timed_span("model.a", trace_id="trace-mine") as measurement:
        measurement.add_attributes(extract_model_usage(first))
    with timed_span("model.b", trace_id="trace-mine") as measurement:
        measurement.add_attributes(extract_model_usage(second))
    with timed_span("model.other", trace_id="trace-other") as measurement:
        measurement.add_attributes(extract_model_usage(other))

    usage = recorder.usage_for("trace-mine")

    assert usage["prompt_tokens"] == 120
    assert usage["completion_tokens"] == 60
    assert usage["total_tokens"] == 180
    assert recorder.usage_for("trace-never-ran") is None


def test_event_sink_done_carries_turn_usage():
    recorder = get_trace_recorder()
    recorder.clear()
    response = SimpleNamespace(usage_metadata={
        "input_tokens": 300,
        "output_tokens": 120,
        "total_tokens": 420,
    })
    with timed_span("model.turn", trace_id="trace-done") as measurement:
        measurement.add_attributes(extract_model_usage(response))

    chunks = []
    sink = EventSink(chunks.append, TurnTiming(), trace_id="trace-done")
    asyncio.run(sink.emit_done({"response": "ok"}))

    done = json.loads(to_sse(chunks[-1]).removeprefix("data: "))
    assert done["event"] == "done"
    assert done["data"]["usage"]["prompt_tokens"] == 300
    assert done["data"]["usage"]["completion_tokens"] == 120


def test_spans_without_explicit_trace_join_the_bound_turn():
    """服务层拿不到 runtime.context，靠 trace_context 把 span 归到本轮。"""
    recorder = get_trace_recorder()
    recorder.clear()
    response = SimpleNamespace(usage_metadata={
        "input_tokens": 40,
        "output_tokens": 10,
        "total_tokens": 50,
    })

    with trace_context("trace-bound"):
        assert current_trace_id() == "trace-bound"
        with timed_span("model.deep") as measurement:
            measurement.add_attributes(extract_model_usage(response))

    assert recorder.usage_for("trace-bound")["total_tokens"] == 50
    assert current_trace_id() == ""
