"""正式性能基线门禁测试，不包含价格假设。"""

from app.evaluation.baseline_gate import BaselineThresholds, validate_baseline


def test_baseline_gate_rejects_example_workload():
    result = validate_baseline(
        {
            "count": 4,
            "success_rate": 1.0,
            "latency_ms": {"p95_ms": 100},
            "baseline_requirements": {"production_workload": False},
        },
        thresholds=BaselineThresholds(),
    )

    assert result["passed"] is False
    assert any("样本量" in item for item in result["failures"])
    assert any("production" in item for item in result["failures"])


def test_baseline_gate_accepts_production_report():
    result = validate_baseline(
        {
            "count": 200,
            "success_rate": 0.995,
            "latency_ms": {"p95_ms": 1200},
            "baseline_requirements": {"production_workload": True},
        },
        thresholds=BaselineThresholds(),
    )

    assert result["passed"] is True
    assert result["failures"] == []
