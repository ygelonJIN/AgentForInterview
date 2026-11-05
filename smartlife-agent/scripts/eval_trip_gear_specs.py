#!/usr/bin/env python3
"""跑规格评测：改前 vs 改后，出对照数字。

用法（在项目根目录）::

    python scripts/eval_trip_gear_specs.py            # 跑默认场景集并写报告
    python scripts/eval_trip_gear_specs.py --no-write # 只看表，不落盘

不调模型、不联网，同一份场景集每次结果一致，适合放进论文附录。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.trip_gear.spec_eval import evaluate, load_scenarios  # noqa: E402

DEFAULT_SCENARIOS = PROJECT_ROOT / "data" / "evaluation" / "trip_gear.spec.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "evaluation" / "trip_gear.spec.report.json"


def _print_table(rows: list) -> None:
    header = f"{'场景':30s} {'期望':12s} {'新判定':12s} {'旧判定':10s} 结果"
    print(header)
    print("-" * len(header))
    marks = {"ok": "ok", "insufficient": "不够", "unknown": "未知", "skip": "不涉及", "missing": "缺"}
    for row in rows:
        verdict = "命中" if row["new_hit"] else "未命中"
        legacy = "命中" if row["legacy_hit"] else "漏判"
        print(
            f"{row['id']:30s} {marks.get(row['expect'], row['expect']):12s} "
            f"{marks.get(row['new'], row['new']):12s} "
            f"{marks.get(row['legacy'], row['legacy']):10s} 新:{verdict} 旧:{legacy}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="规格匹配评测：改前/改后对照")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--no-write", action="store_true", help="只打印，不写报告文件")
    args = parser.parse_args()

    scenarios = load_scenarios(Path(args.scenarios))
    report = evaluate(scenarios)
    summary = report["summary"]

    _print_table(report["rows"])
    print()
    print(f"场景数 {summary['scenarios']}（可判 {summary['judged']}，不涉及 {summary['skipped']}）")
    print(f"新判定准确率 {summary['new_accuracy']:.2%}   旧逻辑准确率 {summary['legacy_accuracy']:.2%}")
    print(f"旧逻辑判出\"不够用\"的次数 {summary['legacy_reported_insufficient']}（结构上做不到）")
    print(f"旧逻辑误判通过 {len(summary['legacy_false_accept'])} 个：{', '.join(summary['legacy_false_accept']) or '无'}")
    print(f"规格未知率 {summary['spec_unknown_rate']:.2%}")
    print(f"新判定分布 {summary['new_distribution']}")

    if args.no_write:
        return

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scenarios_file": str(Path(args.scenarios).relative_to(PROJECT_ROOT)),
        "summary": summary,
        "rows": report["rows"],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {output.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
