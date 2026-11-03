"""可靠性基础组件测试。"""

import asyncio

import pytest

from app.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    TTLCache,
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
