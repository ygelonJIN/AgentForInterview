"""消融实验台的测试：不调模型，用假服务把四组的机制差异钉住。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.evaluation.ablation import (
    Scenario,
    fairness_note,
    load_scenarios,
    run_scenario,
    summarize,
)
from app.trip_gear.models import DayPlan, GearItem, Itinerary, ItineraryItem

SCENARIOS = Path(__file__).resolve().parents[1] / "data" / "evaluation" / "trip_gear.ablation.jsonl"


class _Response:
    usage_metadata = {"input_tokens": 10, "output_tokens": 20}


class _FakeLLM:
    async def ainvoke(self, messages, *args, **kwargs):
        return _Response()


class _FakeService:
    """只实现 ``_plan``：返回固定的行程与装备建议，不碰网络。"""

    def __init__(self, itinerary, gear=None):
        self.llm = _FakeLLM()
        self._itinerary = itinerary
        self._gear = gear or []

    async def _plan(self, facts, constraints):
        # 模拟真实服务"一次模型调用"，让 Recorder 的计数被覆盖到。
        await self.llm.ainvoke([])
        return self._itinerary, list(self._gear)


def _itinerary(cost: float) -> Itinerary:
    return Itinerary(
        destination="杭州",
        days=[
            DayPlan(
                day=1,
                lodging="hotel",
                items=[ItineraryItem(time="10:00", activity="西湖", duration_min=120, cost=cost)],
                lodging_cost=100,
            )
        ],
    )


def _scenario(**overrides) -> Scenario:
    base = dict(
        id="t1",
        message="去杭州玩两天",
        facts={"destination": "杭州", "days": 1, "people": 1},
        products={},
        owned=[],
        expected_gear=[],
    )
    base.update(overrides)
    return Scenario(**base)


def test_场景文件可加载():
    rows = load_scenarios(SCENARIOS)
    assert len(rows) >= 6
    assert all(row.message for row in rows)


def test_G2_有规则装备_G1_没有():
    scenario = _scenario(
        facts={"destination": "杭州", "days": 1, "people": 2, "lodging": "camp", "min_temp_c": 3},
        expected_gear=["帐篷", "睡袋", "防潮垫"],
    )
    service = _FakeService(_itinerary(100))
    g1 = asyncio.run(run_scenario("G1", scenario, service))
    g2 = asyncio.run(run_scenario("G2", scenario, service))

    assert g1["gear_coverage"] == 0.0, "纯提示词基线没有规则装备层"
    assert g2["gear_coverage"] > 0.5, "串行流水线要有规则装备"
    assert g2["gear_count"] >= g1["gear_count"]


def test_G3_会修预算_G2_不会():
    scenario = _scenario(
        message="去杭州玩两天，预算100",
        facts={"destination": "杭州", "days": 1, "people": 1, "budget": 100},
        products={"防晒霜": {"name": "防晒霜", "price": 200, "stock": 9, "subcategory": "防晒"}},
    )
    service = _FakeService(_itinerary(500))
    g2 = asyncio.run(run_scenario("G2", scenario, service))
    g3 = asyncio.run(run_scenario("G3", scenario, service))

    assert g2["repair_rounds"] == 0
    assert g3["repair_rounds"] > 0, "G3 应该砍掉可选项来补缺口"


def test_G4_修不动时把取舍交还用户():
    # 行程里没有可砍的收费项、也没有可选装备：缺口修不动。
    scenario = _scenario(
        message="去杭州玩两天，预算50",
        facts={"destination": "杭州", "days": 1, "people": 1, "budget": 50},
    )
    service = _FakeService(_itinerary(0))
    g3 = asyncio.run(run_scenario("G3", scenario, service))
    g4 = asyncio.run(run_scenario("G4", scenario, service))

    assert g3["asked"] is False
    assert g4["asked"] is True, "没有可执行动作时，G4 应该把取舍交还用户"
    assert g4["repair_rounds"] == 0


def test_四组都记录算力():
    scenario = _scenario(facts={"destination": "杭州", "days": 1, "people": 1})
    service = _FakeService(_itinerary(100))
    row = asyncio.run(run_scenario("G2", scenario, service))
    assert row["calls"] == 1
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 20
    assert row["prompt_chars"] == 0, "假服务不真正发提示词，字符数应为 0 而不是编一个"


def test_汇总按组聚合():
    rows = [
        {"variant": "G1", "v_actual": 2, "v_structural": 1, "missed": ["budget"],
         "disclosed": False, "gear_coverage": 0.0, "budget_deviation": 0.5,
         "repair_rounds": 0, "asked": False, "latency_ms": 100, "prompt_tokens": 10,
         "completion_tokens": 20, "calls": 1, "prompt_chars": 100},
        {"variant": "G1", "v_actual": 0, "v_structural": 0, "missed": [],
         "disclosed": True, "gear_coverage": 1.0, "budget_deviation": 0.0,
         "repair_rounds": 0, "asked": False, "latency_ms": 300, "prompt_tokens": 10,
         "completion_tokens": 20, "calls": 1, "prompt_chars": 200},
    ]
    summary = summarize(rows)["G1"]
    assert summary["mean_violations"] == 1.0
    assert summary["zero_violation_rate"] == 0.5
    assert summary["disclosure_rate"] == 0.0, "只有脏场景进披露率分母"
    assert summary["mean_missed"] == 1.0
    assert summary["mean_prompt_chars"] == 150


def test_公平性把预算写成数字():
    summary = {
        "G1": {"mean_prompt_chars": 900},
        "G2": {"mean_prompt_chars": 1400},
    }
    note = fairness_note(summary)
    assert note["baseline_is_subset"] is True
    assert note["mean_prompt_chars"]["G1"] < note["mean_prompt_chars"]["G2"]
