#!/usr/bin/env python3
"""校验端到端报告是否满足正式上线基线要求。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation.baseline_gate import BaselineThresholds, validate_baseline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--min-samples", type=int, default=100)
    parser.add_argument("--min-success-rate", type=float, default=0.99)
    parser.add_argument("--max-p95-ms", type=float, default=5000.0)
    parser.add_argument("--allow-non-production-workload", action="store_true")
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = validate_baseline(
        report,
        thresholds=BaselineThresholds(
            min_samples=args.min_samples,
            min_success_rate=args.min_success_rate,
            max_p95_ms=args.max_p95_ms,
            require_production_workload=not args.allow_non_production_workload,
        ),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
