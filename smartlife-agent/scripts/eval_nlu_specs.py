#!/usr/bin/env python3
"""跑 NLU 场景集：关键词层 + 槽位 + 装备识别。

用法（在项目根目录）::

    python scripts/eval_nlu_specs.py            # 跑默认场景集并写报告
    python scripts/eval_nlu_specs.py --no-write # 只看表，不落盘

不调模型、不联网，同一份场景集每次结果一致。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.evaluation.nlu_spec import (  # noqa: E402
    DEFAULT_OUTPUT,
    DEFAULT_SCENARIOS,
    evaluate,
    format_report,
    load_cases,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="NLU 场景集评测")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--no-write", action="store_true", help="只打印，不写报告文件")
    parser.add_argument("--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()

    cases = load_cases(Path(args.scenarios))
    report = evaluate(cases)

    if args.verbose:
        for row in report["rows"]:
            mark = "ok" if row["ok"] else "FAIL"
            print(f"{mark:4s} {row['id']:22s} {row['message']}")

    print()
    print(format_report(report))

    if not args.no_write:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {output}")

    return 0 if report["summary"]["mismatched"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
