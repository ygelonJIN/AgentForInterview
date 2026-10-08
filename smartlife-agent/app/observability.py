"""项目统一的结构化日志、异常上下文和轻量 Trace 记录。"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence


logger = logging.getLogger("smartlife")

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider as _OTelTracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor as _OTelBatchSpanProcessor
except Exception:
    _otel_trace = None
    _OTelTracerProvider = None
    _OTelBatchSpanProcessor = None


def _init_otel_tracer():
    if _otel_trace is None:
        return None
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") and _OTelTracerProvider is not None:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            provider = _OTelTracerProvider()
            provider.add_span_processor(_OTelBatchSpanProcessor(OTLPSpanExporter()))
            _otel_trace.set_tracer_provider(provider)
        except Exception as exc:
            logger.warning("OTEL exporter initialization failed: %s", exc)
    return _otel_trace.get_tracer("smartlife.agent")


_otel_tracer = _init_otel_tracer()
_REDACTED_FIELDS = {"api_key", "authorization", "password", "token"}


@dataclass(frozen=True)
class TraceRecord:
    trace_id: str
    span: str
    duration_ms: float
    status: str
    attributes: Dict[str, Any]


class SpanMeasurement:
    """Span 执行期间累积的补充属性。"""

    def __init__(self):
        self.attributes: Dict[str, Any] = {}

    def add_attributes(self, attributes: Optional[Dict[str, Any]]) -> None:
        for key, value in (attributes or {}).items():
            self.attributes[key] = value


class TraceRecorder:
    """进程内有界 Trace 缓冲，用于开发诊断和延迟统计。"""

    def __init__(self, max_records: int = 1000):
        if max_records <= 0:
            raise ValueError("max_records 必须大于 0")
        self.max_records = max_records
        self._records: "deque[TraceRecord]" = deque(maxlen=max_records)
        self._lock = threading.RLock()

    def add(self, record: TraceRecord) -> None:
        with self._lock:
            self._records.append(record)

    def records(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [asdict(record) for record in self._records]

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            records = list(self._records)
        by_span: Dict[str, List[float]] = {}
        errors = 0
        span_usage: Dict[str, Dict[str, float]] = {}
        for record in records:
            by_span.setdefault(record.span, []).append(record.duration_ms)
            if record.status != "ok":
                errors += 1
            usage = span_usage.setdefault(record.span, {
                "prompt_tokens": 0.0,
                "completion_tokens": 0.0,
                "total_tokens": 0.0,
                "cached_prompt_tokens": 0,
                "uncached_prompt_tokens": 0,
            })
            for field in usage:
                value = record.attributes.get(field)
                if isinstance(value, (int, float)):
                    usage[field] += value
        return {
            "count": len(records),
            "errors": errors,
            "spans": {
                span: {
                    "count": len(values),
                    "avg_ms": round(sum(values) / len(values), 3),
                    "max_ms": round(max(values), 3),
                    "p50_ms": round(_percentile(values, 50), 3),
                    "p95_ms": round(_percentile(values, 95), 3),
                    "p99_ms": round(_percentile(values, 99), 3),
                    **{
                        key: round(value, 8)
                        for key, value in span_usage[span].items()
                    },
                }
                for span, values in sorted(by_span.items())
            },
        }

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return ordered[index]


_trace_recorder = TraceRecorder()


def get_trace_recorder() -> TraceRecorder:
    return _trace_recorder


def record_trace(
    span: str,
    duration_ms: float,
    *,
    trace_id: str,
    status: str = "ok",
    attributes: Optional[Dict[str, Any]] = None,
) -> None:
    _trace_recorder.add(TraceRecord(
        trace_id=trace_id,
        span=span,
        duration_ms=float(duration_ms),
        status=status,
        attributes=_safe_context(attributes),
    ))


def extract_model_usage(response: Any) -> Dict[str, Any]:
    """从常见 LangChain/OpenAI 响应结构提取 Token usage，不计算价格。"""
    if response is None:
        return {}

    def detail_value(details: Any, *names: str) -> Optional[int]:
        if isinstance(details, Mapping):
            for name in names:
                value = details.get(name)
                if isinstance(value, (int, float)):
                    return int(value)
        for name in names:
            value = getattr(details, name, None)
            if isinstance(value, (int, float)):
                return int(value)
        return None

    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, Mapping):
        prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
        completion = usage.get("output_tokens", usage.get("completion_tokens"))
        total = usage.get("total_tokens")
        cached = (
            detail_value(
                usage.get("input_token_details", usage.get("prompt_tokens_details")),
                "cached_tokens",
                "cached_input_tokens",
            )
            or usage.get("cached_prompt_tokens", usage.get("cached_input_tokens"))
        )
    else:
        metadata = getattr(response, "response_metadata", None) or {}
        token_usage = metadata.get("token_usage") or metadata.get("usage") or {}
        prompt = token_usage.get("prompt_tokens", token_usage.get("input_tokens"))
        completion = token_usage.get(
            "completion_tokens",
            token_usage.get("output_tokens"),
        )
        total = token_usage.get("total_tokens")
        cached = (
            detail_value(
                token_usage.get("prompt_tokens_details", token_usage.get("input_tokens_details")),
                "cached_tokens",
                "cached_input_tokens",
            )
            or token_usage.get("cached_prompt_tokens", token_usage.get("cached_input_tokens"))
        )

    result: Dict[str, Any] = {}
    if isinstance(prompt, (int, float)):
        result["prompt_tokens"] = int(prompt)
    if isinstance(completion, (int, float)):
        result["completion_tokens"] = int(completion)
    if isinstance(cached, (int, float)):
        result["cached_prompt_tokens"] = min(int(cached), result.get("prompt_tokens", 0))
    if "prompt_tokens" in result:
        result.setdefault("cached_prompt_tokens", 0)
        result["uncached_prompt_tokens"] = max(
            0,
            result["prompt_tokens"] - result["cached_prompt_tokens"],
        )
    if isinstance(total, (int, float)):
        result["total_tokens"] = int(total)
    elif "prompt_tokens" in result and "completion_tokens" in result:
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]

    model_name = (
        getattr(response, "response_metadata", {}).get("model_name")
        or getattr(response, "additional_kwargs", {}).get("model_name")
        or (usage.get("model_name") if isinstance(usage, Mapping) else None)
    )
    if model_name:
        result["model_name"] = str(model_name)

    return result

@contextmanager
def timed_span(
    span: str,
    *,
    trace_id: str,
    attributes: Optional[Dict[str, Any]] = None,
) -> Iterator[SpanMeasurement]:
    measurement = SpanMeasurement()
    started = time.perf_counter()
    otel_span = _otel_tracer.start_span(span) if _otel_tracer else None
    try:
        yield measurement
    except Exception as exc:
        measurement.add_attributes({
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        if otel_span is not None:
            otel_span.set_attribute("error.type", type(exc).__name__)
            otel_span.set_status(_otel_trace.Status(_otel_trace.StatusCode.ERROR, str(exc)))
            otel_span.end()
        record_trace(
            span,
            (time.perf_counter() - started) * 1000,
            trace_id=trace_id,
            status="error",
            attributes={**(attributes or {}), **measurement.attributes},
        )
        raise
    else:
        if otel_span is not None:
            otel_span.set_attribute("trace_id", trace_id)
            for key, value in measurement.attributes.items():
                if isinstance(value, (bool, int, float, str)):
                    otel_span.set_attribute(key, value)
            otel_span.end()
        record_trace(
            span,
            (time.perf_counter() - started) * 1000,
            trace_id=trace_id,
            status="ok",
            attributes={**(attributes or {}), **measurement.attributes},
        )


def _safe_context(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    safe = {}
    for key, value in (context or {}).items():
        if key.lower() in _REDACTED_FIELDS:
            safe[key] = "[redacted]"
        elif isinstance(value, (bool, int, float)):
            safe[key] = value
        else:
            safe[key] = str(value)[:500]
    return safe


def new_trace_id() -> str:
    return uuid.uuid4().hex


def log_exception(component: str, error: Exception, context: Optional[Dict[str, Any]] = None) -> None:
    logger.exception(
        "[%s] %s: %s context=%s",
        component,
        type(error).__name__,
        error,
        _safe_context(context),
    )


def log_warning(component: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
    logger.warning("[%s] %s context=%s", component, message, _safe_context(context))
