#!/usr/bin/env python3
"""跑代价权重敏感性分析：扰动档位/置信度/软权重/τ，看结论翻不翻。

用法（在项目根目录）::

    python scripts/analyze_cost_sensitivity.py
    python scripts/analyze_cost_sensitivity.py --no-write

回答的是答辩必问的那句"权重凭什么"：不解释数字来源，而是给出
"这些数字在多大范围内改，结论都不变"的证据；并明确指出 τ 只影响
"自动改还是问用户"，不影响可不可行。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.trip_gear.sensitivity import analyze  # noqa: E402

DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "evaluation" / "cost.sensitivity.report.json"


def _print(report: dict) -> None:
    baseline = report["baseline"]
    print("基线：")
    print(f"  硬度 hard={baseline['hard_weight']:g} / soft={baseline['soft_weight']:g}")
    print(f"  来源置信度 {baseline['source_confidence']}")
    print(f"  动作档位 {baseline['action_cost']}")
    print(f"  阈值 τ={baseline['tau']:g}")
    print()

    print("1) 动作档位扰动（一次只动一档）")
    for row in report["action_tier"]["perturbations"]:
        plans = "、".join(row["plan_flips"]) or "无"
        autos = "、".join(row["auto_flips"]) or "无"
        print(f"  {row['kind']:16s} ×{row['factor']:<4g} 选择翻转：{plans}；自动/确认翻转：{autos}")
    print(f"  ±20% 内是否稳定：{'是' if report['action_tier']['stable_under_pm20'] else '否'}")
    print(f"  首次翻转倍率：{report['action_tier']['first_flip_factor']}")
    print()

    print("2) 来源置信度（模型推断档）")
    for row in report["source_confidence"]["perturbations"]:
        print(f"  模型={row['model_confidence']:<4g} 披露顺序翻转：{'、'.join(row['order_flips']) or '无'}")
    print()

    print("3) 软约束权重")
    for row in report["soft_weight"]["perturbations"]:
        print(f"  soft={row['soft_weight']:<4g} 代价变化：{'、'.join(row['cost_changed']) or '无'}；"
              f"排序翻转：{'、'.join(row['rank_flips']) or '无'}")
    print()

    print("4) 阈值 τ 扫描（只应改变'自动改/问用户'）")
    for row in report["tau"]["sweep"]:
        print(f"  τ={row['tau']:<4g} 自动性变化 {len(row['auto_changed'])} 个；结论变化 {len(row['state_changed'])} 个")
    print()

    verdict = report["verdict"]
    print("结论：")
    print(f"  ±20% 档位扰动下选择稳定：{'是' if verdict['decisions_stable_under_pm20'] else '否'}")
    print(f"  披露顺序对模型置信度敏感：{'是' if verdict['disclosure_sensitive_to_model_confidence'] else '否'}")
    print(f"  披露顺序对软约束权重敏感：{'是' if verdict['disclosure_sensitive_to_soft_weight'] else '否'}")
    print(f"  τ 是否改变可行性结论：{'是（不该发生）' if verdict['tau_affects_feasibility'] else '否'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="代价权重敏感性分析")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--no-write", action="store_true", help="只打印，不写报告文件")
    args = parser.parse_args()

    report = analyze()
    _print(report)

    if not args.no_write:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "note": "扰动在进程内完成，跑完即恢复；不调模型、不联网。",
            "report": report,
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {output.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
