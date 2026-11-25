"""消融实验台：G1–G4 四组，同一份场景、同一模型、同等提示词预算。

四组的差别只在机制，不在算力
----------------------------
- **G1 纯提示词基线**：一次模型调用，只有事实与原始需求，没有约束清单、
  没有装备规则、不校验不修复；
- **G2 串行流水线**：加规则派生的约束清单 + 规则装备推导（模型补充长尾）；
- **G3 联合修复与可行性判定**：加确定性校验、四态结论与有界修复
  （按"省得多"先修，不看代价）；
- **G4 带代价的提问**：修复改用统一代价排序（``plan_repair``），
  预算缺口修不动时按代价把取舍交还用户（记录 ``asked``）。

公平性怎么保证（论文里要写的那几条）
------------------------------------
1. 同一基座模型、``temperature=0``、同一 ``max_tokens``、同一最大修复轮次；
2. G2–G4 共用同一份事实文本与同一个规划提示词，提示词字符数逐组记录，
   任何一组都拿不到"更长的提示词"；
3. G1 的提示词是 G2 的**子集**（少一份约束清单），不存在基线获得更多预算；
4. 四组共用同一份内联商品目录、同一份已有装备、同一个"今天"，
   不查库、不联网（模型调用除外）；
5. 每组都记 ``prompt_tokens / completion_tokens / 调用次数 / 耗时``，
   读者可以自己判断提升是否只是"多花了算力"。

指标用 ``hard_checker`` 独立数，不由被测链路自报。
"""
from __future__ import annotations

import json
import time
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.observability import extract_model_usage
from app.trip_gear.constraints import derive_constraints
from app.trip_gear.gear import derive_gear
from app.trip_gear.hard_checker import audit, compare, gear_total, trip_total
from app.trip_gear.models import Constraint, GearItem, Itinerary, OwnedItem, TripFacts
from app.trip_gear.owned import apply_owned
from app.trip_gear.repair import RepairAction, apply_action, repair_candidates
from app.trip_gear.verdict import budget_deficit, judge, plan_repair
from app.trip_gear.verify import verify

DEFAULT_TODAY = "2026-11-25"
# verify/purchase 要 date，不是字符串：``days_until`` 会直接做日期减法。
TODAY = date.fromisoformat(DEFAULT_TODAY)

# 修复轮次上限：四组一致，避免"G4 多改几轮"被误读成机制更好。
MAX_REPAIR_ROUNDS = 2

VARIANTS: Dict[str, Dict[str, str]] = {
    "G1": {"label": "纯提示词基线", "detail": "一次模型调用，无约束清单/装备规则/校验"},
    "G2": {"label": "串行流水线", "detail": "+ 规则约束清单 + 规则装备推导"},
    "G3": {"label": "联合修复与可行性判定", "detail": "+ 确定性校验 + 有界修复（按省得多）"},
    "G4": {"label": "带代价的提问", "detail": "+ 统一代价排序 + 缺口交还用户"},
}


@dataclass
class Recorder:
    """给模型套一层，记录调用次数、提示词字符数与 token。"""

    llm: Any
    calls: int = 0
    prompt_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls += 1
        self.prompt_chars += sum(len(str(getattr(message, "content", "") or "")) for message in messages)
        response = await self.llm.ainvoke(messages, *args, **kwargs)
        usage = extract_model_usage(response)
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        return response

    def as_dict(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_chars": self.prompt_chars,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass
class Scenario:
    id: str
    message: str
    facts: Dict[str, Any]
    products: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    owned: List[Dict[str, Any]] = field(default_factory=list)
    expected_gear: List[str] = field(default_factory=list)
    expect_verdict: str = ""


def load_scenarios(path: Path) -> List[Scenario]:
    rows: List[Scenario] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except ValueError as error:
            raise ValueError(f"第 {number} 行不是合法 JSON: {error}") from error
        rows.append(
            Scenario(
                id=str(raw["id"]),
                message=str(raw["message"]),
                facts=dict(raw.get("facts") or {}),
                products=dict(raw.get("products") or {}),
                owned=list(raw.get("owned") or []),
                expected_gear=list(raw.get("expected_gear") or []),
                expect_verdict=str(raw.get("expect_verdict") or ""),
            )
        )
    return rows


def _facts_for(scenario: Scenario) -> TripFacts:
    """规则派生出的事实 + 场景给定事实（天气等外部事实由场景注入）。"""
    derived, _ = derive_constraints(scenario.message, today=DEFAULT_TODAY)
    merged = derived.model_copy(deep=True)
    for key, value in scenario.facts.items():
        if value is None:
            continue
        setattr(merged, key, value)
    return merged


def _constraints_for(scenario: Scenario, facts: TripFacts) -> List[Constraint]:
    _, constraints = derive_constraints(scenario.message, today=DEFAULT_TODAY)
    # 场景给了天气类事实，但天气约束要外部工具才派生；这里按同一口径补一条，
    # 否则"温标"这一族在四组里都测不到。
    if facts.is_camping and facts.min_temp_c is not None:
        text = f"睡袋温标 ≤ {float(facts.min_temp_c) - 5:g}℃"
        if not any(item.key == "sleeping_bag_temp" for item in constraints):
            constraints.append(
                Constraint(text=text, source="规则推导（夜间温度留 5℃ 余量）", key="sleeping_bag_temp")
            )
    if facts.rain_probability is not None and float(facts.rain_probability) >= 40:
        if not any(item.key == "rain" for item in constraints):
            constraints.append(
                Constraint(text=f"降水概率 {facts.rain_probability:g}%，备雨具", source="天气", key="rain")
            )
    return constraints


def _merge_gear(rules: List[GearItem], suggestions: Iterable[GearItem]) -> List[GearItem]:
    """规则清单打底，模型建议按名字合并（同名取数量大的）。"""
    by_name = {item.name: item for item in rules}
    for item in suggestions:
        existing = by_name.get(item.name)
        if existing is None:
            item.status = "need_buy"
            by_name[item.name] = item
            continue
        existing.qty = max(existing.qty, item.qty)
        if item.reason:
            existing.reason = existing.reason or item.reason
    return list(by_name.values())


def _alternatives_finder(products: Dict[str, Dict[str, Any]]):
    def find(subcategory: str = "", max_price: Optional[float] = None, limit: int = 10):
        rows = [
            product for product in products.values()
            if str(product.get("subcategory") or "") == subcategory
            and (max_price is None or float(product.get("price") or 0) <= float(max_price))
        ]
        rows.sort(key=lambda product: float(product.get("price") or 0))
        return rows[:limit]

    return find


def _price_products(gear: List[GearItem], products: Dict[str, Dict[str, Any]]) -> None:
    """把清单里待购的项对齐到内联目录；目录没有的保持未匹配。"""
    for item in gear:
        if item.status != "need_buy":
            continue
        product = products.get(item.name)
        if product and not item.specs:
            item.specs = dict(product.get("specs") or {})


def _coverage(gear: List[GearItem], expected: Iterable[str]) -> float:
    names = [item.name for item in gear if item.status != "skipped"]
    wanted = [name for name in expected if name]
    if not wanted:
        return 1.0
    hit = 0
    for want in wanted:
        if any(want in name or name in want for name in names):
            hit += 1
    return round(hit / len(wanted), 4)


async def run_scenario(variant: str, scenario: Scenario, service: Any) -> Dict[str, Any]:
    """跑一组变体，返回方案指标与算力记录。"""
    facts = _facts_for(scenario)
    products = dict(scenario.products)
    constraints = _constraints_for(scenario, facts) if variant != "G1" else []

    recorder = Recorder(service.llm)
    service.llm = recorder
    started = time.perf_counter()
    try:
        itinerary, suggestions = await service._plan(facts, constraints)
    finally:
        service.llm = recorder.llm
    # 深拷贝：修复会原地改 itinerary/gear，四组共用同一个返回对象时，
    # 前一组改过的方案会被下一组当成输入（测试里踩过，会假性"无违反"）。
    itinerary = itinerary.model_copy(deep=True)
    suggestions = [item.model_copy(deep=True) for item in suggestions]
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if variant == "G1":
        gear = [GearItem(name=item.name, qty=item.qty, reason=item.reason, status="need_buy") for item in suggestions]
    else:
        rules = derive_gear(facts, itinerary, extra_text=scenario.message)
        gear = _merge_gear(rules, suggestions)
    apply_owned(gear, [OwnedItem(**item) for item in scenario.owned])
    _price_products(gear, products)

    repair_rounds = 0
    asked = False
    verdict_state = "feasible"
    declared: List[str] = []
    if variant in ("G3", "G4"):
        finder = _alternatives_finder(products)
        for _ in range(MAX_REPAIR_ROUNDS):
            issues = verify(facts, constraints, itinerary, gear, products, today=TODAY)
            verdict = judge(constraints, issues, repair_options=[], tau=0.15)
            verdict_state = verdict.state
            deficit = budget_deficit(issues)
            if deficit <= 0:
                break
            candidates = [
                action for action in repair_candidates(
                    gear, products, find_alternatives=finder, facts=facts, itinerary=itinerary
                )
                if not action.requires_confirm
            ]
            if not candidates:
                # 没有可执行动作 = 修不动。G3 到此为止（判不可行），
                # G4 把取舍交还用户，这是两组唯一的机制差别之一。
                asked = variant == "G4"
                break
            if variant == "G3":
                chosen = [max(candidates, key=lambda action: action.saving)]
            else:
                plan = plan_repair([action.as_dict() for action in candidates], deficit)
                if plan is None:
                    asked = True
                    break
                chosen = [action for action in candidates if action.name in {item.get("name") for item in plan[1]}]
                if not chosen:
                    break
            for action in chosen:
                apply_action(action, gear, products, itinerary=itinerary, facts=facts)
                repair_rounds += 1
    else:
        verdict_state = "n/a"

    # 披露只算"被测链路自己说出来的"：G1/G2 没有校验节点，结构化披露为空。
    # 这不是评测漏了，而是这两组确实没有"判定 + 披露"这一层。
    if variant in ("G3", "G4"):
        final_issues = verify(facts, constraints, itinerary, gear, products, today=TODAY)
        final_verdict = judge(constraints, final_issues, repair_options=[], tau=0.15)
        verdict_state = final_verdict.state
        declared = sorted({
            issue.code for issue in final_issues
            if issue.severity == "critical" and issue.code
        })

    audited = audit(facts, itinerary, gear, products, today=TODAY)
    diff = compare(audited.codes, declared)
    total = trip_total(itinerary) + gear_total(gear, products)
    budget = float(facts.budget) if facts.budget else 0.0
    return {
        "variant": variant,
        "id": scenario.id,
        "v_actual": audited.count,
        # 把"钱"与"事实"分开：预算类靠改法就能消，结构类只能改需求。
        "v_structural": len([code for code in audited.codes if code not in ("budget", "gear_budget")]),
        "codes": audited.codes,
        "declared_codes": declared,
        "missed": diff["missed"],
        "disclosed": not diff["missed"],
        "gear_coverage": _coverage(gear, scenario.expected_gear),
        "gear_count": len([item for item in gear if item.status != "skipped"]),
        "gear": [item.name for item in gear if item.status != "skipped"],
        "budget_deviation": round((total - budget) / budget, 4) if budget else 0.0,
        "repair_rounds": repair_rounds,
        "asked": asked,
        "verdict_state": verdict_state,
        "latency_ms": elapsed_ms,
        **recorder.as_dict(),
    }


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_variant: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_variant.setdefault(row["variant"], []).append(row)
    summary: Dict[str, Any] = {}
    for variant, items in by_variant.items():
        count = len(items)
        # 披露率只在"确实有违反"的场景上算：干净场景的"已披露"是空真，
        # 放进分母会把基线抬高一截，看着像它也披露了。
        dirty = [item for item in items if item["v_actual"] > 0]
        summary[variant] = {
            "label": VARIANTS.get(variant, {}).get("label", variant),
            "scenarios": count,
            "mean_violations": round(sum(item["v_actual"] for item in items) / count, 4),
            "zero_violation_rate": round(sum(1 for item in items if item["v_actual"] == 0) / count, 4),
            "mean_structural_violations": round(
                sum(item["v_structural"] for item in items) / count, 4
            ),
            # 核心指标：存在违反却没有披露，就是"硬编了一个干净方案"。
            "disclosure_rate": round(
                sum(1 for item in dirty if item["disclosed"]) / len(dirty), 4
            ) if dirty else 1.0,
            "mean_missed": round(
                sum(len(item["missed"]) for item in dirty) / len(dirty), 4
            ) if dirty else 0.0,
            "mean_gear_coverage": round(sum(item["gear_coverage"] for item in items) / count, 4),
            "mean_budget_deviation": round(sum(item["budget_deviation"] for item in items) / count, 4),
            "repair_rounds": sum(item["repair_rounds"] for item in items),
            "asked_rate": round(sum(1 for item in items if item["asked"]) / count, 4),
            "mean_latency_ms": int(sum(item["latency_ms"] for item in items) / count),
            "prompt_tokens": sum(item["prompt_tokens"] for item in items),
            "completion_tokens": sum(item["completion_tokens"] for item in items),
            "calls": sum(item["calls"] for item in items),
            "mean_prompt_chars": int(sum(item["prompt_chars"] for item in items) / count),
        }
    return summary


def fairness_note(summary: Dict[str, Any]) -> Dict[str, Any]:
    """把"同等提示词预算"写成可核对的数字，而不是一句保证。"""
    chars = {variant: data["mean_prompt_chars"] for variant, data in summary.items()}
    return {
        "temperature": 0,
        "max_repair_rounds": MAX_REPAIR_ROUNDS,
        "same_model": True,
        "mean_prompt_chars": chars,
        "baseline_is_subset": (
            chars.get("G1", 0) <= chars.get("G2", 0) if "G1" in chars and "G2" in chars else None
        ),
        "note": (
            "G2–G4 共用同一份事实与规划提示词；G1 的提示词是 G2 的子集"
            "（没有约束清单），因此不存在基线获得更多预算的情况。"
        ),
    }
