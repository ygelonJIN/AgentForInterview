"""端到端延迟、并发和成功率基线。"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


TurnRunner = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


@dataclass
class BenchmarkSample:
    workload_id: str
    latency_ms: float
    ok: bool
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkReport:
    count: int
    success_count: int
    error_count: int
    success_rate: float
    total_duration_ms: float
    throughput_per_second: float
    latency_ms: Dict[str, float]
    samples: List[BenchmarkSample]

    def to_dict(self, *, include_samples: bool = True) -> Dict[str, Any]:
        payload = asdict(self)
        if not include_samples:
            payload.pop("samples", None)
        return payload


def percentile(values: Sequence[float], value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((value / 100) * len(ordered)) - 1)
    return ordered[index]


def parse_workload_jsonl(path: Path) -> List[Dict[str, Any]]:
    workloads: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: JSON 解析失败: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_number}: 每行必须是对象")
            message = str(payload.get("message") or "").strip()
            if not message:
                raise ValueError(f"{path}:{line_number}: message 不能为空")
            workloads.append({
                "id": str(payload.get("id") or f"case-{line_number}"),
                "message": message,
                "user_id": str(payload.get("user_id") or "benchmark-user"),
                "session_id": str(payload.get("session_id") or f"session-{line_number}"),
                "scene": str(payload.get("scene") or "general"),
                "metadata": dict(payload.get("metadata") or {}),
            })
    if not workloads:
        raise ValueError("压测工作负载不能为空")
    return workloads


class BenchmarkRunner:
    def __init__(self, run_turn: TurnRunner):
        self.run_turn = run_turn

    async def run(
        self,
        workloads: Sequence[Mapping[str, Any]],
        *,
        concurrency: int = 1,
    ) -> BenchmarkReport:
        if not workloads:
            raise ValueError("workloads 不能为空")
        if concurrency <= 0:
            raise ValueError("concurrency 必须大于 0")
        semaphore = asyncio.Semaphore(concurrency)
        started = time.perf_counter()

        async def execute(workload: Mapping[str, Any]) -> BenchmarkSample:
            async with semaphore:
                sample_started = time.perf_counter()
                try:
                    result = await self.run_turn(workload)
                    latency = (time.perf_counter() - sample_started) * 1000
                    ok = bool(result.get("ok", True))
                    return BenchmarkSample(
                        workload_id=str(workload.get("id") or ""),
                        latency_ms=latency,
                        ok=ok,
                        error="" if ok else str(result.get("error") or "turn failed"),
                        metadata=dict(result.get("metadata") or {}),
                    )
                except Exception as exc:
                    return BenchmarkSample(
                        workload_id=str(workload.get("id") or ""),
                        latency_ms=(time.perf_counter() - sample_started) * 1000,
                        ok=False,
                        error=f"{type(exc).__name__}: {str(exc)[:300]}",
                    )

        samples = list(await asyncio.gather(*(execute(item) for item in workloads)))
        total_duration_ms = (time.perf_counter() - started) * 1000
        latencies = [sample.latency_ms for sample in samples]
        success_count = sum(1 for sample in samples if sample.ok)
        return BenchmarkReport(
            count=len(samples),
            success_count=success_count,
            error_count=len(samples) - success_count,
            success_rate=success_count / len(samples),
            total_duration_ms=total_duration_ms,
            throughput_per_second=(
                len(samples) / (total_duration_ms / 1000) if total_duration_ms else 0.0
            ),
            latency_ms={
                "avg_ms": sum(latencies) / len(latencies),
                "min_ms": min(latencies),
                "p50_ms": percentile(latencies, 50),
                "p95_ms": percentile(latencies, 95),
                "p99_ms": percentile(latencies, 99),
                "max_ms": max(latencies),
            },
                samples=samples,
        )


def write_report(path: Path, report: BenchmarkReport, **extra: Any) -> None:
    payload = {
        **report.to_dict(include_samples=True),
        **extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
