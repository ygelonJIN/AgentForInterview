"""修复动作的单测：从"砍装备"扩到"改行程"。

重点验证三件事：
1. 换住宿能靠"不买露营三大件"真省下钱，且净省为负时不出这个主意；
2. 改住宿这类动作必须用户确认，不能被自动执行掉；
3. 砍收费项目只动行程，不碰住宿与装备。
"""
from __future__ import annotations

import pytest

from app.trip_gear.models import DayPlan, GearItem, Itinerary, ItineraryItem, TripFacts
from app.trip_gear.repair import (
    RepairAction,
    apply_action,
    repair_candidates,
)
from app.trip_gear.itinerary import lodging_cost_for, total_cost


def _itinerary(people: int = 2, days: int = 2, cost: float = 100.0) -> Itinerary:
    return Itinerary(
        destination="杭州",
        days=[
            DayPlan(
                day=index + 1,
                lodging="camp",
                lodging_cost=lodging_cost_for("camp", people),
                items=[ItineraryItem(time="10:00", activity=f"活动{index + 1}", duration_min=90, cost=cost)],
            )
            for index in range(days)
        ],
    )


def _camping_gear() -> list:
    return [
        GearItem(name="帐篷", status="need_buy"),
        GearItem(name="睡袋", qty=2, status="need_buy"),
        GearItem(name="防潮垫", qty=2, status="need_buy"),
    ]


PRODUCTS = {
    "帐篷": {"name": "双人防水帐篷", "price": 599},
    "睡袋": {"name": "四季保暖睡袋", "price": 299},
    "防潮垫": {"name": "蛋巢防潮垫", "price": 89},
}


class TestLodgingSwap:
    def test_省下的装备钱不够住宿差价时不给建议(self):
        facts = TripFacts(destination="杭州", days=2, people=2, lodging="camp")
        # 装备 599+299*2+89*2 = 1375；2 晚 2 人民宿比露营多 1400，净省 -25。
        actions = repair_candidates(
            _camping_gear(), dict(PRODUCTS), facts=facts, itinerary=_itinerary()
        )
        assert not [a for a in actions if a.kind == "change_lodging"]

    def test_净省为负时不出这个主意(self):
        facts = TripFacts(destination="杭州", days=2, people=2, lodging="camp")
        # 装备只有防潮垫 89*2，省不出住宿差价。
        gear = [GearItem(name="防潮垫", qty=2, status="need_buy")]
        actions = repair_candidates(gear, dict(PRODUCTS), facts=facts, itinerary=_itinerary())
        assert not [a for a in actions if a.kind == "change_lodging"]

    def test_净省为正时给出金额(self):
        facts = TripFacts(destination="杭州", days=1, people=1, lodging="camp")
        # 1 天 1 人：露营 50 -> 民宿 400，多 350。
        # 装备按数量算：599 + 299*2 + 89*2 = 1375，净省 1025。
        actions = repair_candidates(
            _camping_gear(), dict(PRODUCTS), facts=facts, itinerary=_itinerary(people=1, days=1)
        )
        swap = next(a for a in actions if a.kind == "change_lodging")
        assert swap.saving == pytest.approx(1025.0)
        assert swap.requires_confirm is True

    def test_非露营不换住宿(self):
        facts = TripFacts(destination="上海", days=2, people=1, lodging="hotel")
        actions = repair_candidates(_camping_gear(), dict(PRODUCTS), facts=facts, itinerary=_itinerary())
        assert not [a for a in actions if a.kind == "change_lodging"]


class TestSoftenActivity:
    def test_砍最贵的一项(self):
        itinerary = Itinerary(
            destination="杭州",
            days=[
                DayPlan(day=1, items=[
                    ItineraryItem(time="10:00", activity="便宜项目", duration_min=60, cost=60),
                    ItineraryItem(time="14:00", activity="贵的项目", duration_min=60, cost=300),
                ])
            ],
        )
        actions = repair_candidates([], {}, itinerary=itinerary)
        soften = next(a for a in actions if a.kind == "soften_activity")
        assert soften.name == "贵的项目"
        assert soften.saving == pytest.approx(300.0)
        assert soften.requires_confirm is False

    def test_没有收费项目就不提(self):
        itinerary = Itinerary(
            destination="杭州",
            days=[DayPlan(day=1, items=[ItineraryItem(time="10:00", activity="散步", duration_min=60, cost=0)])],
        )
        assert not [a for a in repair_candidates([], {}, itinerary=itinerary) if a.kind == "soften_activity"]


class TestApplyAction:
    def _swap(self):
        return RepairAction(
            kind="change_lodging",
            name="露营改住民宿",
            saving=600.0,
            reason="换住宿",
            requires_confirm=True,
            meta={"to": "hotel", "nights": 2},
        )

    def test_改住宿要用户确认(self):
        gear = _camping_gear()
        products = dict(PRODUCTS)
        with pytest.raises(PermissionError):
            apply_action(self._swap(), gear, products, itinerary=_itinerary())

    def test_确认后改住宿并退掉露营装备(self):
        gear = _camping_gear()
        products = dict(PRODUCTS)
        itinerary = _itinerary()
        before = total_cost(itinerary)

        apply_action(
            self._swap(), gear, products,
            itinerary=itinerary, facts=TripFacts(people=2), force=True,
        )

        assert all(day.lodging == "hotel" for day in itinerary.days)
        assert all(item.status == "skipped" for item in gear)
        assert products == {}
        # 住宿变贵是事实，要能在总费用上体现出来。
        assert total_cost(itinerary) > before

    def test_砍收费项目只动行程(self):
        gear = _camping_gear()
        products = dict(PRODUCTS)
        itinerary = _itinerary()
        action = RepairAction(
            kind="soften_activity", name="活动1", saving=100.0, reason="省钱",
            meta={"day": 1},
        )
        apply_action(action, gear, products, itinerary=itinerary)

        assert [item.activity for item in itinerary.days[0].items] == []
        assert itinerary.days[1].items, "其他天不应被牵连"
        assert products, "装备不该被动"
        assert all(day.lodging == "camp" for day in itinerary.days)


class TestItineraryCost:
    def test_总费用包含住宿(self):
        itinerary = _itinerary(people=2, days=2, cost=100)
        # 条目 2*100 + 住宿 2*100
        assert total_cost(itinerary) == pytest.approx(400.0)

    def test_住宿价按人数与晚数(self):
        assert lodging_cost_for("camp", 2, 2) == pytest.approx(200.0)
        assert lodging_cost_for("hotel", 1, 1) == pytest.approx(400.0)
        assert lodging_cost_for("", 2, 2) == 0.0
