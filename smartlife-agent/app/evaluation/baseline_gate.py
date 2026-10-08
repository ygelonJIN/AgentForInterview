"""性能基线的上线门禁，不包含任何价格或成本假设。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping


@dataclass(frozen=True)
class BaselineThresholds:
    min_samples: int = 100
    min_success_rate: float = 0.99
    max_p95_ms: float = 5000.0
    require_production_workload: bool = True

    def __post_init__(self) -> None:
        if self.min_samples <= 0:
            raise ValueError("min_samples 必须大于 0")
        if not 0 <= self.min_success_rate <= 1:
            raise ValueError("min_success_rate 必须在 0 到 1 之间")
        if self.max_p95_ms <= 0:
            raise ValueError("max_p95_ms 必须大于 0")


def validate_baseline(
    report: Mapping[str, Any],
    *,
    thresholds: BaselineThresholds,
) -> Dict[str, Any]:
    failures = []
    count = int(report.get("count") or 0)
    success_rate = float(report.get("success_rate") or 0)
    latency = dict(report.get("latency_ms") or {})
    p95_ms = float(latency.get("p95_ms") or 0)
    requirements = dict(report.get("baseline_requirements") or {})
    production_workload = bool(requirements.get("production_workload"))

    if count < thresholds.min_samples:
        failures.append(f"样本量 {count} 小于 {thresholds.min_samples}")
    if success_rate < thresholds.min_success_rate:
        failures.append(f"成功率 {success_rate:.4f} 低于 {thresholds.min_success_rate:.4f}")
    if p95_ms > thresholds.max_p95_ms:
        failures.append(f"P95 {p95_ms:.3f}ms 超过 {thresholds.max_p95_ms:.3f}ms")
    if thresholds.require_production_workload and not production_workload:
        failures.append("工作负载不是 production 业务流量，不能作为正式端到端基线")

    return {
        "passed": not failures,
        "failures": failures,
        "observed": {
            "count": count,
            "success_rate": success_rate,
            "p95_ms": p95_ms,
            "production_workload": production_workload,
        },
    }
