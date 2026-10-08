"""可靠性基础组件测试。"""

import asyncio

import pytest

from app.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    NodePolicy,
    RetryableError,
    RetryPolicy,
    TTLCache,
    retry_async,
)


def test_ttl_cache_exposes_stale_values_only_when_requested():
    cache = TTLCache(max_entries=2, ttl_seconds=0.01)
    cache.put("a", {"value": 1})

    assert cache.get("a").value == {"value": 1}
    asyncio.run(asyncio.sleep(0.02))
    assert cache.get("a") is None
    stale = cache.get("a", allow_stale=True)

    assert stale is not None
    assert stale.value == {"value": 1}
    assert stale.fresh is False


def test_circuit_breaker_opens_and_recovers_after_timeout():
    breaker = CircuitBreaker(
        "test",
        failure_threshold=2,
        recovery_timeout_seconds=0.01,
        success_threshold=1,
    )

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: {"ok": True})

    asyncio.run(asyncio.sleep(0.02))
    assert breaker.call(lambda: {"ok": True}) == {"ok": True}
    assert breaker.state.value == "closed"


def test_retry_async_retries_retryable_failures_and_honors_timeout():
    calls = []

    async def flaky(attempt):
        calls.append(attempt)
        if attempt == 1:
            raise RetryableError("temporary")
        return "ok"

    result = asyncio.run(retry_async(
        flaky,
        policy=RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
    ))
    assert result == "ok"
    assert calls == [1, 2]

    async def slow(_attempt):
        await asyncio.sleep(0.05)
        return "late"

    with pytest.raises(asyncio.TimeoutError, match="超过 0.01s"):
        asyncio.run(retry_async(
            slow,
            policy=RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
            timeout_seconds=0.01,
        ))


def test_retry_async_exposes_attempt_retry_and_limit_events():
    events = []

    async def flaky(attempt):
        if attempt < 3:
            raise RetryableError(f"temporary-{attempt}")
        return "ok"

    result = asyncio.run(retry_async(
        flaky,
        policy=RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0),
        on_event=lambda kind, payload: events.append((kind, payload)),
    ))

    assert result == "ok"
    kinds = [kind for kind, _ in events]
    assert kinds.count("attempt_started") == 3
    assert kinds.count("attempt_failed") == 2
    assert kinds.count("retry_scheduled") == 2
    assert kinds.count("attempt_succeeded") == 1
    assert events[-1][1]["attempt"] == 3


def test_retry_async_reports_non_retryable_failure_without_retry():
    events = []

    async def fail(_attempt):
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        asyncio.run(retry_async(
            fail,
            policy=RetryPolicy(max_attempts=3),
            on_event=lambda kind, payload: events.append((kind, payload)),
        ))

    failed = next(payload for kind, payload in events if kind == "attempt_failed")
    assert failed["retryable"] is False
    assert failed["will_retry"] is False
    assert [kind for kind, _ in events].count("attempt_started") == 1


def test_node_policy_applies_timeout():
    policy = NodePolicy(timeout_seconds=0.01, retry_policy=RetryPolicy(max_attempts=1))

    async def slow(_attempt):
        await asyncio.sleep(0.05)
        return "late"

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(policy.run(slow))
