#!/usr/bin/env python3
"""运行真实主链路端到端并发基线并输出 P50/P95/P99。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path
from typing import Any, Dict, Mapping

from app.evaluation.e2e_benchmark import BenchmarkRunner, parse_workload_jsonl, write_report
from app.observability import get_trace_recorder
from app.tools.providers import get_provider_health


def _event_payloads(sse: str):
    for line in sse.splitlines():
        if line.startswith("data: "):
            try:
                payload = json.loads(line.removeprefix("data: "))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


async def _run_chat_turn(orchestrator, workload: Mapping[str, Any]) -> Dict[str, Any]:
    events = []
    async for line in orchestrator.process_streaming(
        workload["message"],
        workload["user_id"],
        workload["session_id"],
        scene=workload["scene"],
    ):
        events.extend(_event_payloads(line))
    errors = [event for event in events if event.get("event") == "error"]
    done = [event for event in events if event.get("event") == "done"]
    return {
        "ok": bool(done) and not errors,
        "error": errors[-1].get("data", {}).get("message", "") if errors else "",
        "metadata": {"event_count": len(events), "done_count": len(done)},
    }


async def _run_retrieval_turn(service, workload: Mapping[str, Any]) -> Dict[str, Any]:
    metadata = workload.get("metadata") or {}
    result = await asyncio.to_thread(
        service.retrieve,
        workload["message"],
        strategy=str(metadata.get("strategy") or "mixed"),
        top_k=int(metadata.get("top_k") or 5),
        owner=workload.get("user_id"),
    )
    return {
        "ok": not result.get("sql_error") and not result.get("rag_error"),
        "error": str(result.get("sql_error") or result.get("rag_error") or ""),
        "metadata": {
            "selected_sources": result.get("selected_sources"),
            "evidence_coverage": result.get("evidence_coverage"),
            "product_count": len(result.get("products") or []),
            "document_count": len(result.get("documents") or []),
        },
    }


async def run(args) -> Dict[str, Any]:
    workloads = parse_workload_jsonl(args.workload)
    recorder = get_trace_recorder()
    recorder.clear()

    if args.mode == "chat":
        from app.agents.langgraph_orchestrator import get_langgraph_orchestrator

        orchestrator = get_langgraph_orchestrator()
        runner = BenchmarkRunner(lambda workload: _run_chat_turn(orchestrator, workload))
    else:
        from app.retrieval.service import RetrievalService

        service = RetrievalService(cache_ttl_seconds=0)
        runner = BenchmarkRunner(lambda workload: _run_retrieval_turn(service, workload))

    report = await runner.run(workloads, concurrency=args.concurrency)
    records = recorder.records()
    trace_summary = recorder.summary()
    payload = {
        "mode": args.mode,
        "concurrency": args.concurrency,
        "workload": str(args.workload),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "provider_health": get_provider_health(),
        "trace_summary": trace_summary,
        "baseline_requirements": {
            "production_workload": all(
                str((item.get("metadata") or {}).get("environment")) == "production"
                for item in workloads
            )
            and bool(workloads),
        },
    }
    write_report(args.output, report, **payload)
    return {"report": report.to_dict(include_samples=False), **payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", required=True, type=Path)
    parser.add_argument("--mode", choices=("chat", "retrieval"), default="chat")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.concurrency <= 0:
        raise SystemExit("--concurrency 必须大于 0")
    payload = asyncio.run(run(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
