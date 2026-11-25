#!/usr/bin/env python3
"""跑 G1–G4 消融实验：同一份场景、同一模型、同等提示词预算。

用法（在项目根目录）::

    python scripts/run_ablation.py                    # 全部场景 × 四组
    python scripts/run_ablation.py --limit 2          # 先拿两条场景试跑
    python scripts/run_ablation.py --variants G1,G4   # 只对比首尾两组
    python scripts/run_ablation.py --no-write         # 只看表，不落盘

会真实调用模型（temperature=0）。报告里同时给质量指标与算力指标，
并附一段可核对的"公平性"数字（每组平均提示词字符数、调用次数、token）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.evaluation.ablation import (  # noqa: E402
    VARIANTS,
    fairness_note,
    load_scenarios,
    run_scenario,
    summarize,
)

DEFAULT_SCENARIOS = PROJECT_ROOT / "data" / "evaluation" / "trip_gear.ablation.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "evaluation" / "trip_gear.ablation.report.json"


def _print_table(rows: list) -> None:
    header = (
        f"{'组':4s} {'场景':16s} {'违反':>4s} {'结构':>4s} {'漏披露':>6s} "
        f"{'装备覆盖':>8s} {'预算偏差':>8s} {'修复轮':>6s} {'提问':>4s} "
        f"{'tokens':>7s} {'耗时ms':>7s}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        tokens = int(row["prompt_tokens"]) + int(row["completion_tokens"])
        print(
            f"{row['variant']:4s} {row['id']:16s} {row['v_actual']:4d} {row['v_structural']:4d} "
            f"{len(row['missed']):6d} "
            f"{row['gear_coverage']:8.2f} {row['budget_deviation']:8.2f} "
            f"{row['repair_rounds']:6d} {'是' if row['asked'] else '否':>4s} "
            f"{tokens:7d} {row['latency_ms']:7d}"
        )


def _print_summary(summary: dict, fairness: dict) -> None:
    print()
    header = (
        f"{'组':4s} {'名称':22s} {'平均违反':>8s} {'零违反率':>8s} {'结构违反':>8s} "
        f"{'披露率':>7s} {'漏披露':>7s} {'装备覆盖':>8s} {'预算偏差':>8s} "
        f"{'轮次':>4s} {'提问率':>6s} {'提示词字符':>10s} {'token':>8s}"
    )
    print(header)
    print("-" * len(header))
    for variant, data in summary.items():
        tokens = data["prompt_tokens"] + data["completion_tokens"]
        print(
            f"{variant:4s} {data['label']:22s} {data['mean_violations']:8.2f} "
            f"{data['zero_violation_rate']:8.2%} {data['mean_structural_violations']:8.2f} "
            f"{data['disclosure_rate']:7.2%} {data['mean_missed']:7.2f} "
            f"{data['mean_gear_coverage']:8.2%} "
            f"{data['mean_budget_deviation']:8.2%} {data['repair_rounds']:4d} "
            f"{data['asked_rate']:6.2%} {data['mean_prompt_chars']:10d} {tokens:8d}"
        )
    print()
    print("公平性：", json.dumps(fairness, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="G1–G4 消融实验")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--variants", default="G1,G2,G3,G4")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条场景")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    from app.trip_gear.service import TripGearService

    variants = [item.strip() for item in args.variants.split(",") if item.strip()]
    unknown = [item for item in variants if item not in VARIANTS]
    if unknown:
        raise SystemExit(f"未知的组：{unknown}，可选 {list(VARIANTS)}")

    scenarios = load_scenarios(Path(args.scenarios))
    if args.limit > 0:
        scenarios = scenarios[: args.limit]

    async def _run_all() -> list:
        # 一个事件循环跑完：httpx 客户端绑在创建它的循环上，
        # 每条场景各开一次 asyncio.run 会让第二次调用直接"Event loop is closed"，
        # 结果是悄悄回落到 fallback 行程，看起来像模型输出很差。
        service = TripGearService()
        rows = []
        for variant in variants:
            for scenario in scenarios:
                row = await run_scenario(variant, scenario, service)
                rows.append(row)
                print(f"完成 {variant} {scenario.id}：违反 {row['v_actual']}，"
                      f"覆盖 {row['gear_coverage']:.2f}，轮次 {row['repair_rounds']}")
        return rows

    rows = asyncio.run(_run_all())

    summary = summarize(rows)
    fairness = fairness_note(summary)
    _print_table(rows)
    _print_summary(summary, fairness)

    if not args.no_write:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "scenarios_file": str(Path(args.scenarios).relative_to(PROJECT_ROOT)),
            "variants": {key: VARIANTS[key] for key in variants},
            "fairness": fairness,
            "summary": summary,
            "rows": rows,
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {output.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
