"""统一的超时、重试、熔断和 TTL 缓存基础组件。"""

from __future__ import annotations

import copy
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
