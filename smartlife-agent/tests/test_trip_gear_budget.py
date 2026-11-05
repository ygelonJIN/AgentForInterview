"""预算占用的单测：花多少、剩多少、余额能换成什么。

用户给预算说的是"打算花这么多"，不是"别超过"。余额不说明白等于把决定权
丢了；说明白了就别再编"加个活动大概 200"这种算不出来的建议——这里只允许
出现算得出来的选项。
"""
from __future__ import annotations

import pytest

from app.trip_gear.budget import UNDERUSE_RATIO, snapshot
from app.trip_gear.itinerary import lodging_cost_for
from app.trip_gear.models import DayPlan, GearItem, Itinerary, ItineraryItem, TripFacts


def _itinerary(people=2, days=2, cost=100.0) -> Itinerary:
    return Itinerary(
        destination="杭州",
        days=[
            DayPlan(
                day=index + 1,
                lodging="camp",
                lodging_cost=lodging_cost_for("camp", people),
                items=[ItineraryItem(time="10:00", activity="活动", duration_min=90, cost=cost)],
            )
            for index in range(days)
        ],
    )


PRODUCTS = {
    "帐篷": {"name": "双人防水帐篷", "price": 599},
    "睡袋": {"name": "四季保暖睡袋", "price": 299},
    "防潮垫": {"name": "蛋巢防潮垫", "price": 89},
}


def _gear():
    return [
        GearItem(name="帐篷", status="need_buy"),
        GearItem(name="睡袋", qty=2, status="need_buy"),
        GearItem(name="防潮垫", qty=2, status="need_buy"),
    ]


class TestSnapshot:
    def test_占用与余额(self):
        facts = TripFacts(budget=5000)
        snap = snapshot(facts, _itinerary(), _gear(), PRODUCTS)
        assert snap.trip == pytest.approx(400.0)
        assert snap.equipment == pytest.approx(599 + 299 * 2 + 89 * 2)
        assert snap.remaining == pytest.approx(5000 - snap.total)

    def test_预算用不满会被标出来(self):
        facts = TripFacts(budget=5000)
        snap = snapshot(facts, _itinerary(), _gear(), PRODUCTS)
        assert snap.ratio_left >= UNDERUSE_RATIO
        assert snap.underused is True

    def test_预算刚好够不算没用满(self):
        snap = snapshot(TripFacts(budget=1400), _itinerary(), _gear(), PRODUCTS)
        assert snap.underused is False

    def test_没有预算时余额为空(self):
        snap = snapshot(TripFacts(), _itinerary(), _gear(), PRODUCTS)
        assert snap.remaining is None
        assert snap.underused is False

    def test_未匹配的装备单独列出(self):
        snap = snapshot(TripFacts(budget=5000), _itinerary(), _gear(), {})
        assert set(snap.unknown) == {"帐篷", "睡袋", "防潮垫"}
