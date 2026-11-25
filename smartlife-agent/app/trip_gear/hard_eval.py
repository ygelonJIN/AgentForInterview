"""硬约束评测：场景集 → 检查器实际结果 vs 系统披露结果。

两层指标，别混着看：

1. **M1 检查器一致率**：独立检查器算出来的违反集合 == 人工标注的
   ``expect_codes``。它证明"尺子"是准的，与被测系统无关；
2. **M2 披露一致性**：把同一份方案喂给被测的 ``verify`` + ``judge``，
   比较它披露的硬违反与检查器实际认定的违反。``missed`` 是漏报，
   ``false_alarm`` 是虚报。

场景文件 ``data/evaluation/trip_gear.hard.jsonl`` 一行一个场景，
``expect_codes`` / ``expect_verdict`` 由人工标注，不从被测代码生成。
方案（itinerary/gear/products）也写在场景里，评测不查库、不联网、不调模型，
同一份场景集任何时候跑出来的数字都一样。

不可行场景的正确性怎么算
------------------------
不可行场景的硬违反数**不可能为 0**——那正是"不可行"的含义。所以对这类
场景不看"违反数为 0"，看三条：

- ``verdict == infeasible``（判得对）；
- ``v_actual > 0``（确实存在违反，不是空判）；
- ``missed == []``（存在的违反都被披露了，没有硬编一个干净方案）。

可行场景才是"违反数 = 0 或全部披露"。两类分开统计，不混进一个准确率。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List

from app.trip_gear.hard_checker import CODES, audit, compare
from app.trip_gear.models import Constraint, GearItem, Itinerary, TripFacts
from app.trip_gear.verdict import judge
from app.trip_gear.verify import verify

EXPECT_VERDICTS = ("feasible", "repairable", "needs_input", "infeasible")

# 评测时"今天"固定，避免场景随真实日期漂移。
DEFAULT_TODAY = "2026-11-25"

# "不可判定"也算一种义务：没披露等于假装没有问题。
UNDECIDABLE_CODES = ("sleeping_bag_temp", "spec_unknown", "lead_unknown")


def load_scenarios(path: Path) -> List[Dict[str, Any]]:
    """读 JSONL；格式错误直接报错，不静默少跑。"""
    scenarios: List[Dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except ValueError as error:
                raise ValueError(f"第 {number} 行不是合法 JSON: {error}") from error
            if not item.get("id"):
                raise ValueError(f"第 {number} 行缺少 id")
            if item.get("expect_verdict") not in EXPECT_VERDICTS:
                raise ValueError(
                    f"{item.get('id')} 的 expect_verdict={item.get('expect_verdict')!r} 不在 {EXPECT_VERDICTS} 里"
                )
            unknown = set(item.get("expect_codes") or []) - set(CODES)
            if unknown:
                raise ValueError(f"{item.get('id')} 的 expect_codes 含未知类别 {sorted(unknown)}")
            scenarios.append(item)
    return scenarios


def _today_of(scenario: Dict[str, Any]) -> date:
    return date.fromisoformat(str(scenario.get("today") or DEFAULT_TODAY)[:10])


def build_case(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """把一行场景还原成被测的数据结构。"""
    facts = TripFacts(**dict(scenario.get("facts") or {}))
    constraints = [Constraint(**item) for item in scenario.get("constraints") or []]
    itinerary = Itinerary(**dict(scenario.get("itinerary") or {}))
    gear = [GearItem(**item) for item in scenario.get("gear") or []]
    products = dict(scenario.get("products") or {})
    return {
        "facts": facts,
        "constraints": constraints,
        "itinerary": itinerary,
        "gear": gear,
        "products": products,
    }


def run_case(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """跑一份场景：检查器（M1）+ 被测链路（M2）。"""
    case = build_case(scenario)
    facts, constraints = case["facts"], case["constraints"]
    itinerary, gear, products = case["itinerary"], case["gear"], case["products"]
    today = _today_of(scenario)

    audited = audit(facts, itinerary, gear, products, today=today)
    issues = verify(facts, constraints, itinerary, gear, products, today=today)
    verdict = judge(
        constraints,
        issues,
        repair_options=scenario.get("repair_options") or None,
        tau=scenario.get("tau"),
    )

    declared = sorted({issue.code for issue in issues if issue.severity == "critical" and issue.code})
    disclosed_undecidable = {
        issue.code for issue in issues if issue.code in UNDECIDABLE_CODES
    }
    diff = compare(audited.codes, declared)
    return {
        "id": scenario["id"],
        "note": scenario.get("note", ""),
        "expect_verdict": scenario["expect_verdict"],
        "verdict": verdict.state,
        "verdict_ok": verdict.state == scenario["expect_verdict"],
        "expect_codes": sorted(set(scenario.get("expect_codes") or [])),
        "actual_codes": sorted(set(audited.codes)),
        "checker_ok": sorted(set(audited.codes)) == sorted(set(scenario.get("expect_codes") or [])),
        "v_actual": audited.count,
        "v_declared": len(declared),
        "declared_codes": declared,
        "missed": diff["missed"],
        "false_alarm": diff["false_alarm"],
        "unchecked": diff["unchecked"],
        "consistent": not diff["missed"] and not diff["false_alarm"],
        "undecidable": audited.undecidable,
        "undecidable_disclosed": sorted(set(audited.undecidable) & disclosed_undecidable),
        "undecidable_ok": set(audited.undecidable) <= disclosed_undecidable,
    }


def _rate(hits: Iterable[bool]) -> float:
    values = list(hits)
    return round(sum(1 for value in values if value) / len(values), 4) if values else 0.0


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把逐行结果折成两类场景分开报的数字。"""
    feasible = [row for row in rows if row["expect_verdict"] == "feasible"]
    infeasible = [row for row in rows if row["expect_verdict"] == "infeasible"]
    others = [row for row in rows if row["expect_verdict"] not in ("feasible", "infeasible")]
    distribution: Dict[str, int] = {}
    for row in rows:
        distribution[row["expect_verdict"]] = distribution.get(row["expect_verdict"], 0) + 1
    return {
        "scenarios": len(rows),
        "expect_distribution": distribution,
        # M1：尺子本身准不准
        "checker_accuracy": _rate(row["checker_ok"] for row in rows),
        # M2：判得对不对、披露得全不全
        "verdict_accuracy": _rate(row["verdict_ok"] for row in rows),
        "disclosure_consistency": _rate(row["consistent"] for row in rows),
        "missed_total": sum(len(row["missed"]) for row in rows),
        "false_alarm_total": sum(len(row["false_alarm"]) for row in rows),
        "unchecked_total": sum(len(row["unchecked"]) for row in rows),
        "feasible": {
            "count": len(feasible),
            # 可行场景：违反数为 0 的比例（干净方案）
            "zero_violation_rate": _rate(row["v_actual"] == 0 for row in feasible),
            # 违反了但全部披露：也算合格，单独报
            "disclosed_rate": _rate(row["consistent"] for row in feasible),
            "total_violations": sum(row["v_actual"] for row in feasible),
        },
        "infeasible": {
            "count": len(infeasible),
            # 不可行场景：判成 infeasible 的比例
            "detected_rate": _rate(row["verdict"] == "infeasible" for row in infeasible),
            # 判成 infeasible 且违反都被披露（"空判不可行"不算过）
            "consistent_rate": _rate(
                row["verdict"] == "infeasible" and row["consistent"] and row["v_actual"] > 0
                for row in infeasible
            ),
            # 误报可行：最危险的失败模式，必须为 0
            "false_positive_feasible": sum(
                1 for row in infeasible if row["verdict"] == "feasible"
            ),
        },
        "other_states": {
            "count": len(others),
            "consistent_rate": _rate(row["consistent"] for row in others),
        },
        "undecidable": {
            "count": sum(1 for row in rows if row["undecidable"]),
            "disclosed_rate": _rate(row["undecidable_ok"] for row in rows if row["undecidable"]),
        },
    }


def evaluate(scenarios: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [run_case(scenario) for scenario in scenarios]
    return {"rows": rows, "summary": summarize(rows)}
