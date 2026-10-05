"""统一异常日志和敏感上下文脱敏测试。"""

import logging

from app.observability import log_exception, log_warning


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
