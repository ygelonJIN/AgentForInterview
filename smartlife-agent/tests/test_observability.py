"""统一异常日志和敏感上下文脱敏测试。"""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app.observability import (
    get_trace_recorder,
    extract_model_usage,
    log_exception,
    log_warning,
    timed_span,
)
from app.streaming import EventQueue


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


def test_event_queue_carries_stable_trace_id_in_sse():
    async def run():
        queue = EventQueue(trace_id="trace-fixed")
        await queue.emit("step", {"name": "test"})
        await queue.finish()
        return [line async for line in queue.to_sse()]

    lines = asyncio.run(run())
    payloads = [json.loads(line.removeprefix("data: ")) for line in lines]

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
