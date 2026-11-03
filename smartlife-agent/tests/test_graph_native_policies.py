"""LangGraph 原生重试/超时语义回归。

这些能力以前由自研 ``run_policy_node`` / ``NodePolicy`` 提供，现在由
``add_node(..., retry_policy=..., timeout=...)`` 承担。本文件锁定其行为，
避免有人把自研包装器加回来。
"""
import asyncio
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.reliability import RetryableError


class _State(TypedDict, total=False):
    attempts: int
    done: bool


def test_native_retry_policy_retries_retryable_error():
    calls = []

    async def flaky(state: _State) -> dict:
        calls.append(1)
        if len(calls) == 1:
            raise RetryableError("临时故障")
        return {"done": True}

    graph = StateGraph(_State)
    graph.add_node(
        "flaky",
        flaky,
        retry_policy=RetryPolicy(max_attempts=2, initial_interval=0.01, retry_on=RetryableError),
    )
    graph.add_edge(START, "flaky")
    graph.add_edge("flaky", END)
    compiled = graph.compile()

    result = asyncio.run(compiled.ainvoke({}))

    assert result["done"] is True
    assert len(calls) == 2


def test_native_timeout_cancels_slow_async_node():
    async def slow(_state: _State) -> dict:
        await asyncio.sleep(0.2)
        return {"done": True}

    graph = StateGraph(_State)
    graph.add_node("slow", slow, timeout=0.01)
    graph.add_edge(START, "slow")
    graph.add_edge("slow", END)
    compiled = graph.compile()

    with pytest.raises(Exception):
        asyncio.run(compiled.ainvoke({}))


def test_non_retryable_error_is_not_retried():
    calls = []

    async def boom(_state: _State) -> dict:
        calls.append(1)
        raise ValueError("不可重试")

    graph = StateGraph(_State)
    graph.add_node(
        "boom",
        boom,
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.01, retry_on=RetryableError),
    )
    graph.add_edge(START, "boom")
    graph.add_edge("boom", END)
    compiled = graph.compile()

    with pytest.raises(ValueError):
        asyncio.run(compiled.ainvoke({}))
    assert len(calls) == 1
