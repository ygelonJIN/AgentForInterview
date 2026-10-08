"""统一的超时、重试、熔断和 TTL 缓存基础组件。"""

from __future__ import annotations

import asyncio
import copy
import inspect
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Generic, Optional, TypeVar


T = TypeVar("T")


class RetryableError(RuntimeError):
    """可安全重试的临时故障。"""


class CircuitOpenError(RetryableError):
    """熔断器已打开，暂时禁止访问下游。"""


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    base_delay_seconds: float = 0.05
    max_delay_seconds: float = 1.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError("max_attempts 必须大于 0")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("重试延迟不能为负数")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds 不能小于 base_delay_seconds")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio 必须在 0 到 1 之间")

    def delay_for(self, retry_index: int) -> float:
        if retry_index <= 0:
            return 0.0
        base = min(
            self.base_delay_seconds * (2 ** (retry_index - 1)),
            self.max_delay_seconds,
        )
        if not self.jitter_ratio:
            return base
        jitter = base * self.jitter_ratio
        return max(0.0, base + random.uniform(-jitter, jitter))


@dataclass
class CacheEntry(Generic[T]):
    value: T
    created_at: float
    fresh: bool


class TTLCache(Generic[T]):
    """线程安全的有界 TTL 缓存，过期值可作为 stale fallback 读取。"""

    def __init__(self, max_entries: int = 128, ttl_seconds: float = 60.0):
        if max_entries <= 0:
            raise ValueError("max_entries 必须大于 0")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds 不能为负数")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: "OrderedDict[str, CacheEntry[T]]" = OrderedDict()
        self._lock = threading.RLock()
        self._stats = {"hits": 0, "misses": 0, "stale_hits": 0, "evictions": 0}

    def get(self, key: str, *, allow_stale: bool = False) -> Optional[CacheEntry[T]]:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._stats["misses"] += 1
                return None
            fresh = self.ttl_seconds > 0 and now - entry.created_at < self.ttl_seconds
            if fresh:
                self._entries.move_to_end(key)
                self._stats["hits"] += 1
                return CacheEntry(copy.deepcopy(entry.value), entry.created_at, True)
            if allow_stale:
                self._entries.move_to_end(key)
                self._stats["stale_hits"] += 1
                return CacheEntry(copy.deepcopy(entry.value), entry.created_at, False)
            self._stats["misses"] += 1
            return None

    def put(self, key: str, value: T) -> None:
        if self.ttl_seconds <= 0:
            return
        with self._lock:
            self._entries[key] = CacheEntry(copy.deepcopy(value), time.monotonic(), True)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self._stats["evictions"] += 1

    def invalidate(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {**self._stats, "entries": len(self._entries)}


class CircuitBreaker:
    """按连续失败次数打开、定时进入半开状态的同步/异步熔断器。"""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 15.0,
        success_threshold: int = 1,
    ):
        if failure_threshold <= 0:
            raise ValueError("failure_threshold 必须大于 0")
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds 必须大于 0")
        if success_threshold <= 0:
            raise ValueError("success_threshold 必须大于 0")
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.success_threshold = success_threshold
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at = 0.0
        self._lock = threading.RLock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and time.monotonic() - self._opened_at >= self.recovery_timeout_seconds
        ):
            self._state = CircuitState.HALF_OPEN
            self._success_count = 0

    def before_call(self) -> None:
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN:
                raise CircuitOpenError(f"{self.name} 熔断器已打开")

    def record_success(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            else:
                self._state = CircuitState.CLOSED
                self._failure_count = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._success_count = 0
            if (
                self._state is CircuitState.HALF_OPEN
                or self._failure_count >= self.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    def call(self, func: Callable[[], T]) -> T:
        self.before_call()
        try:
            result = func()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    async def call_async(self, func: Callable[[], Awaitable[T]]) -> T:
        self.before_call()
        try:
            result = await func()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._maybe_half_open()
            return {
                "name": self.name,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
            }


async def _notify_retry_event(
    on_event: Optional[Callable[[str, Dict[str, Any]], Any]],
    event: str,
    payload: Dict[str, Any],
) -> None:
    if on_event is None:
        return
    result = on_event(event, payload)
    if inspect.isawaitable(result):
        await result


async def retry_async(
    operation: Callable[[int], Awaitable[T]],
    *,
    policy: RetryPolicy,
    retry_for: tuple[type[BaseException], ...] = (RetryableError,),
    timeout_seconds: Optional[float] = None,
    on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
) -> T:
    """按尝试次数执行异步操作；超时和指定异常可重试。

    ``on_event`` 会报告 attempt_started、attempt_succeeded、retry_scheduled、
    attempt_failed 和 retry_exhausted，调用方据此生成可见执行日志。
    """
    last_error: Optional[BaseException] = None
    retryable_exceptions = (asyncio.TimeoutError, *retry_for)

    for attempt in range(1, policy.max_attempts + 1):
        started = time.monotonic()
        await _notify_retry_event(on_event, "attempt_started", {
            "attempt": attempt,
            "max_attempts": policy.max_attempts,
            "timeout_seconds": timeout_seconds,
        })
        try:
            coroutine = operation(attempt)
            if timeout_seconds is None:
                result = await coroutine
            else:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise asyncio.TimeoutError("操作已超过总超时时间")
                result = await asyncio.wait_for(coroutine, timeout=remaining)
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError) and not str(exc):
                timeout_label = (
                    f"操作超过 {timeout_seconds:.3g}s"
                    if timeout_seconds is not None else "操作超时"
                )
                exc = asyncio.TimeoutError(timeout_label)
            duration_ms = (time.monotonic() - started) * 1000
            retryable = isinstance(exc, retryable_exceptions)
            delay = policy.delay_for(attempt) if retryable else 0.0
            timeout_would_expire = (
                timeout_seconds is not None
                and (time.monotonic() - started) + delay >= timeout_seconds
            )
            will_retry = retryable and attempt < policy.max_attempts and not timeout_would_expire
            await _notify_retry_event(on_event, "attempt_failed", {
                "attempt": attempt,
                "max_attempts": policy.max_attempts,
                "duration_ms": round(duration_ms, 3),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "retryable": retryable,
                "will_retry": will_retry,
            })
            if not retryable:
                raise
            last_error = exc
            if will_retry:
                await _notify_retry_event(on_event, "retry_scheduled", {
                    "attempt": attempt,
                    "next_attempt": attempt + 1,
                    "max_attempts": policy.max_attempts,
                    "delay_seconds": round(delay, 3),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                if delay:
                    await asyncio.sleep(delay)
                continue
            break
        else:
            await _notify_retry_event(on_event, "attempt_succeeded", {
                "attempt": attempt,
                "max_attempts": policy.max_attempts,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            })
            return result

    assert last_error is not None
    await _notify_retry_event(on_event, "retry_exhausted", {
        "attempts": attempt,
        "max_attempts": policy.max_attempts,
        "error_type": type(last_error).__name__,
        "error": str(last_error),
    })
    raise last_error


class NodePolicy:
    """LangGraph 节点级超时和重试策略。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        retry_policy: Optional[RetryPolicy] = None,
        retryable_exceptions: tuple[type[BaseException], ...] = (RetryableError,),
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self.timeout_seconds = timeout_seconds
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=1)
        self.retryable_exceptions = retryable_exceptions

    async def run(
        self,
        operation: Callable[[int], Awaitable[T]],
        *,
        on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> T:
        return await retry_async(
            operation,
            policy=self.retry_policy,
            retry_for=self.retryable_exceptions,
            timeout_seconds=self.timeout_seconds,
            on_event=on_event,
        )
