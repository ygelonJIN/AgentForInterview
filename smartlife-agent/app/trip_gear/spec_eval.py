"""规格评测：场景集 + 改前/改后对照。

只做确定性计算，不调模型、不碰网络，同一份场景集任何时候跑出来的数字都一样。

``legacy_bag_verdict`` 复刻改造前的判定逻辑——**只看 ``reason`` 里有没有
"温标"两个字**，从不比较数值。保留它是为了让"改前 / 改后"的差异可复现，
而不是靠回忆。生产链路禁止调用它；这里能存在，是因为它只活在评测里。

场景集格式（``data/evaluation/trip_gear.spec.jsonl``，一行一个）::

    {
      "id": "camp-cold-weak-bag",
      "message": "周末去杭州露营",
      "facts": {"destination": "杭州", "days": 2, "people": 2,
                "lodging": "camp", "min_temp_c": 3},
      "owned": [{"name": "睡袋", "specs": {"comfort_temp_c": 10}}],
      "products": {"睡袋": {"name": "某睡袋", "price": 299, "specs": {...}}},
      "expect": "insufficient",   # ok / insufficient / unknown / skip
      "note": "夜间 3℃ 要求 ≤ -2℃，10℃ 不够"
    }

``expect`` 是人工标注的期望结论，不是用被测代码算出来的——否则就是自己
给自己出题。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.trip_gear.gear import derive_gear
from app.trip_gear.models import (
    Constraint,
    DayPlan,
    GearItem,
    Itinerary,
    ItineraryItem,
    OwnedItem,
    TripFacts,
)
from app.trip_gear.owned import apply_owned
from app.trip_gear.spec_match import TEMPERATURE_MARGIN_C
from app.trip_gear.verify import verify

# 期望结论的合法取值。skip 表示这个场景不该产生温标要求。
EXPECTATIONS = ("ok", "insufficient", "unknown", "skip")

# 新判定（约束状态）到评测结论的映射。
STATUS_TO_VERDICT = {
    "ok": "ok",
    "violated": "insufficient",
    "unverified": "unknown",
}


def load_scenarios(path: Path) -> List[Dict[str, Any]]:
    """读 JSONL 场景集；空行跳过，格式错误直接报错而不是静默少跑。"""
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
            if item.get("expect") not in EXPECTATIONS:
                raise ValueError(
                    f"{item.get('id')} 的 expect={item.get('expect')!r} 不在 {EXPECTATIONS} 里"
                )
            scenarios.append(item)
    return scenarios


def _itinerary(facts: TripFacts) -> Itinerary:
    days = int(facts.days or 1)
    lodging = "camp" if facts.is_camping else "hotel"
    return Itinerary(
        destination=facts.destination,
        days=[
            DayPlan(
                day=index + 1,
                lodging=lodging,
                items=[ItineraryItem(time="10:00", activity="行程", duration_min=120, cost=100)],
            )
            for index in range(days)
        ],
    )


def _bag_constraint(facts: TripFacts) -> Optional[Constraint]:
    """按与 ``constraints.py`` 相同的口径派生温标约束。

    这里不直接调 ``enrich_weather``（那要网络），但公式与余量必须一致，
    否则评测测的就是另一套规则。
    """
    if not facts.is_camping or facts.min_temp_c is None:
        return None
    return Constraint(
        text=f"睡袋温标 ≤ {facts.min_temp_c - TEMPERATURE_MARGIN_C:.0f}℃",
        source="规则推导（夜间温度留 5℃ 余量）",
        key="sleeping_bag_temp",
    )


def build_case(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """把一个场景还原成可直接校验的数据结构。"""
    facts = TripFacts(**scenario.get("facts", {}))
    itinerary = _itinerary(facts)
    gear = derive_gear(facts, itinerary, extra_text=str(scenario.get("message") or ""))
    owned = [OwnedItem(**item) for item in scenario.get("owned", [])]
    apply_owned(gear, owned)
    constraint = _bag_constraint(facts)
    constraints = [constraint] if constraint else []
    return {
        "facts": facts,
        "itinerary": itinerary,
        "gear": gear,
        "constraints": constraints,
        "products": dict(scenario.get("products") or {}),
    }


def new_bag_verdict(case: Dict[str, Any]) -> str:
    """跑真实校验器，取温标约束的结论。"""
    constraints = case["constraints"]
    if not constraints:
        return "skip"
    verify(
        case["facts"],
        constraints,
        case["itinerary"],
        case["gear"],
        case["products"],
    )
    return STATUS_TO_VERDICT.get(constraints[0].status, "unknown")


def legacy_bag_verdict(gear: Iterable[GearItem]) -> str:
    """改造前的判定：只看理由里有没有"温标"，不比较数值。

    返回值：``missing``（清单无睡袋）/ ``unknown``（理由里没写温标）/
    ``pass``（其余一律当满足）。注意它**结构上不可能**返回 insufficient。
    """
    bag = next((item for item in gear if item.name == "睡袋"), None)
    if bag is None:
        return "missing"
    if "温标" not in (bag.reason or ""):
        return "unknown"
    return "pass"


def _legacy_hit(expect: str, legacy: str) -> bool:
    if expect == "skip":
        return True
    if expect == "ok":
        return legacy == "pass"
    # 不够用 / 未知 都不是 pass，旧逻辑一律算没判对。
    return legacy == "unknown" and expect == "unknown"


def evaluate(scenarios: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """逐场景对照，返回可直接写进报告的结构。"""
    rows: List[Dict[str, Any]] = []
    for scenario in scenarios:
        case = build_case(scenario)
        expect = scenario["expect"]
        fresh = new_bag_verdict(case)
        legacy = "skip" if expect == "skip" else legacy_bag_verdict(case["gear"])
        rows.append(
            {
                "id": scenario["id"],
                "expect": expect,
                "new": fresh,
                "legacy": legacy,
                "new_hit": fresh == expect,
                "legacy_hit": _legacy_hit(expect, legacy),
                "note": scenario.get("note", ""),
            }
        )
    return {"rows": rows, "summary": summarize(rows)}


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows)
    judged = [row for row in rows if row["expect"] != "skip"]

    def rate(key: str) -> float:
        if not judged:
            return 0.0
        return round(sum(1 for row in judged if row[key]) / len(judged), 4)

    distribution = {name: 0 for name in EXPECTATIONS}
    for row in rows:
        distribution[row["new"]] = distribution.get(row["new"], 0) + 1

    missed = [row["id"] for row in rows if row["expect"] == "insufficient" and row["new"] != "insufficient"]
    false_accept = [
        row["id"] for row in rows
        if row["expect"] in {"insufficient", "unknown"} and row["legacy"] == "pass"
    ]
    return {
        "scenarios": total,
        "judged": len(judged),
        "skipped": total - len(judged),
        "new_accuracy": rate("new_hit"),
        "legacy_accuracy": rate("legacy_hit"),
        "new_distribution": distribution,
        "new_missed_insufficient": missed,
        "legacy_false_accept": false_accept,
        "legacy_reported_insufficient": sum(1 for row in rows if row["legacy"] == "insufficient"),
        "spec_unknown_rate": round(
            distribution.get("unknown", 0) / len(judged), 4
        ) if judged else 0.0,
    }
