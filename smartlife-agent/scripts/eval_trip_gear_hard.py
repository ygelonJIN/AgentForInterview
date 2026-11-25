#!/usr/bin/env python3
"""跑硬约束评测：独立检查器（M1）+ 系统披露（M2）。

用法（在项目根目录）::

    python scripts/eval_trip_gear_hard.py             # 跑默认场景集并写报告
    python scripts/eval_trip_gear_hard.py --no-write  # 只看表，不落盘

两层数字分开看：

- **M1 检查器一致率**：检查器算出的违反 == 人工标注 ``expect_codes``，
  证明"尺子"本身是准的；
- **M2 披露一致性**：被测 ``verify`` 披露的硬违反 vs 检查器实际认定的违反。
  ``missed`` 是漏报，``false_alarm`` 是虚报。

不调模型、不联网、不查库；``today`` 由场景固定，任何一天跑结果都一样。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.trip_gear.hard_eval import evaluate, load_scenarios  # noqa: E402

DEFAULT_SCENARIOS = PROJECT_ROOT / "data" / "evaluation" / "trip_gear.hard.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "evaluation" / "trip_gear.hard.report.json"

_MARKS = {
    "feasible": "可行",
    "repairable": "可修复",
    "needs_input": "待补充",
    "infeasible": "不可行",
}


def _print_table(rows: list) -> None:
    header = f"{'场景':28s} {'期望':8s} {'结论':8s} {'V实际':>5s} {'V披露':>5s} {'漏':>3s} {'虚':>3s} 一致"
    print(header)
    print("-" * len(header))
    for row in rows:
        mark = "是" if row["consistent"] else "否"
        print(
            f"{row['id']:28s} {_MARKS.get(row['expect_verdict'], row['expect_verdict']):8s} "
            f"{_MARKS.get(row['verdict'], row['verdict']):8s} "
            f"{row['v_actual']:5d} {row['v_declared']:5d} "
            f"{len(row['missed']):3d} {len(row['false_alarm']):3d} {mark}"
        )
        if row["missed"] or row["false_alarm"]:
            print(f"{'':28s} 实际={row['actual_codes']} 披露={row['declared_codes']}")


def _print_summary(summary: dict) -> None:
    print()
    print(f"场景数 {summary['scenarios']}，分布 {summary['expect_distribution']}")
    print(f"M1 检查器一致率 {summary['checker_accuracy']:.2%}（尺子准不准）")
    print(f"M2 结论准确率   {summary['verdict_accuracy']:.2%}")
    print(f"M2 披露一致性   {summary['disclosure_consistency']:.2%}"
          f"（漏报 {summary['missed_total']}，虚报 {summary['false_alarm_total']}，"
          f"词表外披露 {summary['unchecked_total']}）")
    feasible = summary["feasible"]
    print(f"可行场景 {feasible['count']} 个：违反数为 0 占 {feasible['zero_violation_rate']:.2%}，"
          f"披露合格占 {feasible['disclosed_rate']:.2%}，共 {feasible['total_violations']} 条违反")
    infeasible = summary["infeasible"]
    print(f"不可行场景 {infeasible['count']} 个：判为不可行 {infeasible['detected_rate']:.2%}，"
          f"判对且披露一致 {infeasible['consistent_rate']:.2%}，"
          f"误报可行 {infeasible['false_positive_feasible']} 个")
    other = summary["other_states"]
    print(f"可修复/待补充 {other['count']} 个：披露一致 {other['consistent_rate']:.2%}")
    undecidable = summary["undecidable"]
    print(f"不可判定项 {undecidable['count']} 个：已披露 {undecidable['disclosed_rate']:.2%}")


def main() -> int:
    parser = argparse.ArgumentParser(description="硬约束评测：检查器 + 披露一致性")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--no-write", action="store_true", help="只打印，不写报告文件")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="有任何一条不一致就以退出码 1 结束（适合放进 CI）",
    )
    args = parser.parse_args()

    scenarios = load_scenarios(Path(args.scenarios))
    report = evaluate(scenarios)
    summary = report["summary"]

    _print_table(report["rows"])
    _print_summary(summary)

    if not args.no_write:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "scenarios_file": str(Path(args.scenarios).relative_to(PROJECT_ROOT)),
            "metric_definitions": {
                "V_actual": "独立检查器认定的硬约束违反条数（每个约束家族最多 1 条）",
                "V_declared": "被测链路披露的 critical issue 去重条数",
                "missed": "检查器认定违反、系统未披露",
                "false_alarm": "系统披露违反、检查器认定不成立",
                "feasible.zero_violation_rate": "可行场景里 V_actual=0 的比例",
                "infeasible.detected_rate": "不可行场景判成 infeasible 的比例",
                "infeasible.consistent_rate": "判对且 V_actual>0 且披露一致的比例",
            },
            "summary": summary,
            "rows": report["rows"],
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {output.relative_to(PROJECT_ROOT)}")

    if args.strict:
        problems = [
            row for row in report["rows"]
            if not (row["checker_ok"] and row["verdict_ok"] and row["consistent"])
        ]
        if problems:
            print(f"\n{len(problems)} 个场景未达标，退出码 1")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
