"""端到端压测基线工具测试。"""

import asyncio
import subprocess
import sys
from pathlib import Path

from app.evaluation.e2e_benchmark import BenchmarkRunner, percentile


def test_percentile_and_benchmark_report():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4, 5], 95) == 5

    async def run_turn(workload):
        await asyncio.sleep(0.001 * (int(workload["id"].split("-")[-1]) % 3))
        return {
            "ok": workload["id"] != "case-3",
            "error": "boom" if workload["id"] == "case-3" else "",
        }

    report = asyncio.run(BenchmarkRunner(run_turn).run(
        [{"id": f"case-{index}", "message": "hello"} for index in range(1, 6)],
        concurrency=2,
    ))

    assert report.count == 5
    assert report.success_count == 4
    assert report.error_count == 1
    assert report.success_rate == 0.8
    assert report.latency_ms["p95_ms"] >= report.latency_ms["p50_ms"]


def test_benchmark_report_aggregates_turn_and_node_timings():
    async def run_turn(workload):
        return {
            "ok": True,
            "metadata": {
                "timing_ms": {
                    "time_to_first_token_ms": 10,
                    "response_ready_ms": 20,
                    "pipeline_complete_ms": 30,
                },
                "node_durations_ms": {
                    "shopping:generate": [12, 18],
                },
            },
        }

    report = asyncio.run(BenchmarkRunner(run_turn).run(
        [{"id": "case-1", "message": "hello"}],
        concurrency=1,
    ))

    assert report.metric_latency_ms["timing.time_to_first_token_ms"]["p95_ms"] == 10
    assert report.metric_latency_ms["timing.pipeline_complete_ms"]["p95_ms"] == 30
    assert report.metric_latency_ms["node.shopping:generate"]["count"] == 2


def test_benchmark_cli_entrypoint_starts():
    script = Path(__file__).parents[1] / "scripts" / "run_e2e_benchmark.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "--workload" in result.stdout
