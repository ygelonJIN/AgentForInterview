"""端到端压测基线工具测试。"""

import asyncio

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
