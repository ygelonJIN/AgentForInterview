"""硬约束检查器与披露一致性指标：先把"尺子"钉住，再测指标会不会报警。

两类测试分开：

- 检查器测试只喂数据、不跑被测链路，保证它独立可复现；
- 指标测试用 monkeypatch 故意制造"漏披露"，证明一致率不是恒为 100%
  （一条恒真断言比没有断言更糟）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.trip_gear.hard_checker import audit, compare
from app.trip_gear.hard_eval import evaluate, load_scenarios, run_case
from app.trip_gear.models import Constraint, DayPlan, GearItem, Itinerary, ItineraryItem, TripFacts


def _itinerary(costs=(100.0,), lodging="hotel", lodging_cost=0.0, times=None):
    items = []
    for index, cost in enumerate(costs):
        start = (times or ["10:00"] * len(costs))[index]
        items.append(ItineraryItem(time=start, activity=f"活动{index + 1}", duration_min=120, cost=cost))
    return Itinerary(
        destination="杭州",
        days=[DayPlan(day=1, lodging=lodging, items=items, lodging_cost=lodging_cost)],
    )


class Test检查器:
    def test_预算超支只按约束计一条(self):
        facts = TripFacts(destination="杭州", days=2, budget=1000)
        itinerary = Itinerary(
            destination="杭州",
            days=[
                DayPlan(day=1, items=[ItineraryItem(time="10:00", activity="A", duration_min=60, cost=800)]),
                DayPlan(day=2, items=[ItineraryItem(time="10:00", activity="B", duration_min=60, cost=800)]),
            ],
        )
        result = audit(facts, itinerary)
        assert result.codes == ["budget"]
        assert result.count == 1

    def test_刚好等于预算不算违反(self):
        facts = TripFacts(destination="杭州", days=1, budget=1000)
        result = audit(facts, _itinerary(costs=(1000.0,)))
        assert result.violations == []

    def test_行程预算口径排除装备(self):
        facts = TripFacts(destination="杭州", days=1, budget=500, budget_scope="trip_only")
        gear = [GearItem(name="冲锋衣", status="need_buy")]
        products = {"冲锋衣": {"name": "冲锋衣", "price": 900}}
        result = audit(facts, _itinerary(costs=(100.0,)), gear, products)
        assert result.violations == []

    def test_装备预算单独核对(self):
        facts = TripFacts(destination="杭州", days=1, gear_budget=500)
        gear = [GearItem(name="冲锋衣", status="need_buy")]
        products = {"冲锋衣": {"name": "冲锋衣", "price": 900}}
        result = audit(facts, _itinerary(costs=(0.0,)), gear, products)
        assert result.codes == ["gear_budget"]

    def test_温标未知不算违反但算不可判定(self):
        facts = TripFacts(destination="杭州", lodging="camp", min_temp_c=3, days=1)
        gear = [
            GearItem(name="帐篷", status="owned", specs={"capacity_persons": 2}),
            GearItem(name="睡袋", status="owned", specs={}),
            GearItem(name="防潮垫", status="owned", specs={"r_value": 2.0}),
        ]
        result = audit(facts, _itinerary(costs=(0.0,), lodging="camp"), gear)
        assert result.violations == []
        assert "sleeping_bag_temp" in result.undecidable

    def test_温标不够是违反(self):
        facts = TripFacts(destination="杭州", lodging="camp", min_temp_c=3, days=1, people=1)
        gear = [
            GearItem(name="帐篷", status="owned", specs={"capacity_persons": 1}),
            GearItem(name="睡袋", status="owned", specs={"comfort_temp_c": 10}),
            GearItem(name="防潮垫", status="owned", specs={"r_value": 2.0}),
        ]
        result = audit(facts, _itinerary(costs=(0.0,), lodging="camp"), gear)
        assert result.codes == ["sleeping_bag_temp"]

    def test_帐篷容量不够(self):
        facts = TripFacts(destination="杭州", lodging="camp", days=1, people=3)
        gear = [
            GearItem(name="帐篷", status="owned", specs={"capacity_persons": 2}),
            GearItem(name="睡袋", status="owned", specs={"comfort_temp_c": -5}),
            GearItem(name="防潮垫", status="owned", specs={"r_value": 2.0}),
        ]
        result = audit(facts, _itinerary(costs=(0.0,), lodging="camp"), gear)
        assert "spec_insufficient" in result.codes

    def test_防潮垫保温不够(self):
        facts = TripFacts(destination="杭州", lodging="camp", min_temp_c=3, days=1, people=1)
        gear = [
            GearItem(name="帐篷", status="owned", specs={"capacity_persons": 1}),
            GearItem(name="睡袋", status="owned", specs={"comfort_temp_c": -5}),
            GearItem(name="防潮垫", status="owned", specs={"r_value": 0.5}),
        ]
        result = audit(facts, _itinerary(costs=(0.0,), lodging="camp"), gear)
        assert "spec_insufficient" in result.codes

    def test_露营必备缺一件只计一条(self):
        facts = TripFacts(destination="杭州", lodging="camp", days=1, min_temp_c=10)
        gear = [GearItem(name="帐篷", status="owned", specs={"capacity_persons": 1})]
        result = audit(facts, _itinerary(costs=(0.0,), lodging="camp"), gear)
        assert result.codes == ["camping_gear"]

    def test_要求露营却住酒店(self):
        facts = TripFacts(destination="杭州", lodging="camp", days=1, min_temp_c=10)
        gear = [
            GearItem(name="帐篷", status="owned", specs={"capacity_persons": 1}),
            GearItem(name="睡袋", status="owned", specs={"comfort_temp_c": 0}),
            GearItem(name="防潮垫", status="owned", specs={"r_value": 2.0}),
        ]
        result = audit(facts, _itinerary(costs=(0.0,), lodging="hotel"), gear)
        assert result.codes == ["lodging"]

    def test_同一时间段重叠只计一条(self):
        facts = TripFacts(destination="杭州", days=1)
        itinerary = Itinerary(
            destination="杭州",
            days=[DayPlan(day=1, items=[
                ItineraryItem(time="10:00", activity="游船", duration_min=120, cost=0),
                ItineraryItem(time="11:00", activity="寺庙", duration_min=90, cost=0),
            ])],
        )
        result = audit(facts, itinerary)
        assert result.codes == ["time_overlap"]

    def test_无货优先于时效(self):
        facts = TripFacts(destination="杭州", days=1, start_date="2026-12-01")
        gear = [GearItem(name="冲锋衣", status="need_buy")]
        products = {"冲锋衣": {"name": "冲锋衣", "price": 900, "stock": 0, "lead_time_days": 1}}
        result = audit(facts, _itinerary(costs=(0.0,)), gear, products, today=date(2026, 11, 25))
        assert result.codes == ["out_of_stock"]

    def test_到货赶不上出发(self):
        facts = TripFacts(destination="杭州", days=1, start_date="2026-11-28")
        gear = [GearItem(name="登山鞋", status="need_buy")]
        products = {"登山鞋": {"name": "登山鞋", "price": 600, "stock": 5, "lead_time_days": 7}}
        result = audit(facts, _itinerary(costs=(0.0,)), gear, products, today=date(2026, 11, 25))
        assert result.codes == ["lead_time"]

    def test_没有出发日期就不判时效(self):
        facts = TripFacts(destination="杭州", days=1)
        gear = [GearItem(name="登山鞋", status="need_buy")]
        products = {"登山鞋": {"name": "登山鞋", "price": 600, "stock": 5, "lead_time_days": 7}}
        result = audit(facts, _itinerary(costs=(0.0,)), gear, products, today=date(2026, 11, 25))
        assert result.violations == []


class Test披露对账:
    def test_漏披露与虚报分开(self):
        diff = compare(["budget", "sleeping_bag_temp"], ["budget", "lead_time"])
        assert diff["missed"] == ["sleeping_bag_temp"]
        assert diff["false_alarm"] == ["lead_time"]

    def test_词表外的披露单列(self):
        diff = compare(["budget"], ["budget", "people"])
        assert diff["unchecked"] == ["people"]


SCENARIOS = Path(__file__).resolve().parents[1] / "data" / "evaluation" / "trip_gear.hard.jsonl"


def _scenarios():
    return load_scenarios(SCENARIOS)


class Test评测指标:
    def test_场景集先过检查器(self):
        report = evaluate(_scenarios())
        summary = report["summary"]
        assert summary["checker_accuracy"] == 1.0
        assert summary["false_alarm_total"] == 0

    def test_漏披露会让一致率掉下来(self):
        """故意让 verify 丢掉一条披露，指标必须报警而不是恒为 100%。"""
        import app.trip_gear.hard_eval as hard_eval

        original = hard_eval.verify

        def dropping_verify(facts, constraints, itinerary, gear, products, **kwargs):
            issues = original(facts, constraints, itinerary, gear, products, **kwargs)
            return [issue for issue in issues if issue.code != "budget"]

        hard_eval.verify = dropping_verify
        try:
            report = evaluate(_scenarios())
        finally:
            hard_eval.verify = original

        assert report["summary"]["missed_total"] > 0
        assert report["summary"]["disclosure_consistency"] < 1.0

    def test_不可行场景要判对且违反数大于零(self):
        rows = {row["id"]: row for row in evaluate(_scenarios())["rows"]}
        for scenario_id in ("bag-temp-weak", "out-of-stock", "lead-time-miss", "lodging-mismatch"):
            row = rows[scenario_id]
            assert row["verdict"] == "infeasible"
            assert row["v_actual"] > 0
            assert row["missed"] == []

    def test_可行场景违反数为零(self):
        rows = [row for row in evaluate(_scenarios())["rows"] if row["expect_verdict"] == "feasible"]
        assert rows
        for row in rows:
            assert row["v_actual"] == 0
            assert row["verdict"] == "feasible"

    def test_未知规格必须披露(self):
        row = {item["id"]: item for item in evaluate(_scenarios())["rows"]}["camp-ok-unknown-bag"]
        assert row["undecidable"] == ["sleeping_bag_temp"]
        assert row["undecidable_ok"] is True


class Test自由命名:
    def test_带修饰词的装备名也算有(self):
        """模型/商品名带修饰词（"双人帐篷"）不能判成缺装备。"""
        facts = TripFacts(destination="杭州", lodging="camp", days=1, people=2, min_temp_c=10)
        gear = [
            GearItem(name="双人帐篷", status="owned", specs={"capacity_persons": 2}),
            GearItem(name="-5℃ 羽绒睡袋", status="owned", specs={"comfort_temp_c": -5}),
            GearItem(name="加厚防潮垫", status="owned", specs={"r_value": 2.5}),
        ]
        result = audit(facts, _itinerary(costs=(0.0,), lodging="camp"), gear)
        assert "camping_gear" not in result.codes
