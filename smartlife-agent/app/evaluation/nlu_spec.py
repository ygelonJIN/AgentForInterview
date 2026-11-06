"""NLU 场景集评测：关键词层、槽位抽取、装备识别。

只测确定性部分（不调模型、不联网），所以同一份场景集任何时候结果一致。
小模型和仲裁不在覆盖范围内——它们的输入输出本身不确定。

用法::

    python scripts/eval_nlu_specs.py            # 跑场景集，有不符合就退出码 1
    python scripts/eval_nlu_specs.py --no-write # 只打印，不写报告
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_SCENARIOS = PROJECT_ROOT / "data" / "evaluation" / "nlu.spec.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "evaluation" / "nlu.spec.report.json"


def load_cases(path: Path) -> List[Dict[str, Any]]:
    cases = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


def predict(message: str, today: str = "") -> Dict[str, Any]:
    """按线上真实顺序算出可确定的预测值。"""
    from app.classifier import _keyword_classify
    from app.nlu.slots import extract_slots
    from app.trip_gear.intent import is_gear_request

    keyword = _keyword_classify(message)
    slots = extract_slots(message, today=today or None)

    if keyword is None:
        intent = None
        intents: List[str] = []
        route = None
    else:
        intents = list(keyword.get("intents") or [keyword["intent"]])
        intent = "composite" if len(intents) > 1 else keyword["intent"]
        route = keyword["route"]

    return {
        "intent": intent,
        "intents": intents,
        "route": route,
        "gear": is_gear_request(message),
        "destination": slots.destination,
        "days": slots.days,
        "people": slots.people,
        "budget": slots.budget,
        "start_date": slots.start_date,
        "lodging": slots.lodging,
        "with_kids": slots.with_kids,
    }


def evaluate(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = []
    mismatches = 0
    for case in cases:
        predicted = predict(case["message"], case.get("today", ""))
        expect = case.get("expect") or {}
        diff = {
            key: {"expect": value, "actual": predicted.get(key)}
            for key, value in expect.items()
            if predicted.get(key) != value
        }
        if diff:
            mismatches += 1
        rows.append({
            "id": case["id"],
            "message": case["message"],
            "ok": not diff,
            "diff": diff,
            "note": case.get("note", ""),
            "predicted": predicted,
        })
    total = len(rows)
    return {
        "summary": {
            "cases": total,
            "matched": total - mismatches,
            "mismatched": mismatches,
            "accuracy": round((total - mismatches) / total, 4) if total else 0.0,
        },
        "rows": rows,
    }


def format_report(report: Dict[str, Any]) -> str:
    lines = []
    for row in report["rows"]:
        if row["ok"]:
            continue
        lines.append(f"[{row['id']}] {row['message']}")
        for key, detail in row["diff"].items():
            lines.append(f"    {key}: 期望 {detail['expect']!r} / 实际 {detail['actual']!r}")
        if row["note"]:
            lines.append(f"    备注：{row['note']}")
    summary = report["summary"]
    head = (
        f"场景 {summary['cases']} 命中 {summary['matched']} "
        f"不符 {summary['mismatched']} 准确率 {summary['accuracy']:.2%}"
    )
    return "\n".join([head] + ([""] + lines if lines else []))
