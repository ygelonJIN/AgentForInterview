"""校验器的集成测试：规格不够必须真的报出来。

这个文件是 ``verify`` 的第一份测试。此前它一条都没有，于是"夜里 6℃、
睡袋温标 10℃、照样判通过"这种问题能一直躲着。
"""
from __future__ import annotations

import pytest

from app.trip_gear.models import (
    Constraint,
    DayPlan,
    GearItem,
    Itinerary,
    ItineraryItem,
    TripFacts,
)
from app.trip_gear.verify import verify


def _itinerary(days: int = 2) -> Itinerary:
    return Itinerary(
        destination="杭州",
        days=[
            DayPlan(
                day=index + 1,
                lodging="camp",
                items=[ItineraryItem(time="10:00", activity="露营", duration_min=120, cost=100)],
            )
            for index in range(days)
        ],
    )


def _facts(**overrides) -> TripFacts:
    base = dict(destination="杭州", days=2, people=2, lodging="camp", min_temp_c=3)
    base.update(overrides)
    return TripFacts(**base)


def _bag_constraint() -> Constraint:
    return Constraint(
        text="睡袋温标 ≤ -2℃",
        source="规则推导（夜间温度留 5℃ 余量）",
        key="sleeping_bag_temp",
    )


def _gear(bag_specs=None, bag_status="owned", tent_specs=None) -> list:
    return [
        GearItem(
            name="帐篷",
            status="owned",
            specs={"capacity_persons": 2} if tent_specs is None else tent_specs,
        ),
        GearItem(name="睡袋", status=bag_status, specs=bag_specs or {}),
        GearItem(name="防潮垫", status="owned", specs={"r_value": 2.5}),
    ]


class TestSleepingBagTemperature:
    def test_温标不够必须报违规(self):
        # 核心回归：10℃ 温标的睡袋，夜间 3℃（要求 ≤ -2℃），必须判违规。
        constraints = [_bag_constraint()]
        issues = verify(
            _facts(), constraints, _itinerary(), _gear({"comfort_temp_c": 10}), {}
        )
        assert constraints[0].status == "violated"
        critical = [issue for issue in issues if issue.severity == "critical"]
        assert any("睡袋" in str(issue.constraint) for issue in critical)
        assert any("10" in str(issue.actual) for issue in critical)

    def test_温标达标不报(self):
        constraints = [_bag_constraint()]
        issues = verify(
            _facts(), constraints, _itinerary(), _gear({"comfort_temp_c": -5}), {}
        )
        assert constraints[0].status == "ok"
        assert not [i for i in issues if "睡袋" in str(i.constraint) and i.severity == "critical"]

    def test_温标未知标为未校验而不是通过(self):
        constraints = [_bag_constraint()]
        issues = verify(_facts(), constraints, _itinerary(), _gear({}), {})
        assert constraints[0].status == "unverified"
        assert not [i for i in issues if i.severity == "critical" and "睡袋" in str(i.constraint)]
        assert any("温标不明" in str(issue.hint) for issue in issues)

    def test_理由里写了温标不算证据(self):
        # 旧实现只查 reason 里有没有"温标"两个字，这里防止它被改回去。
        bag = GearItem(
            name="睡袋",
            status="owned",
            reason="夏季薄款，温标 15℃",
            specs={},
        )
        constraints = [_bag_constraint()]
        verify(_facts(), constraints, _itinerary(), [*_gear({})[:1], bag, _gear({})[2]], {})
        assert constraints[0].status == "unverified"

    def test_待购睡袋用商品规格判断(self):
        constraints = [_bag_constraint()]
        products = {"睡袋": {"name": "四季保暖睡袋", "price": 299, "specs": '{"comfort_temp_c": -5}'}}
        issues = verify(
            _facts(), constraints, _itinerary(), _gear({}, bag_status="need_buy"), products
        )
        assert constraints[0].status == "ok"
        assert not [i for i in issues if i.severity == "critical" and "睡袋" in str(i.constraint)]


class TestTentCapacity:
    def test_三人露营两顶帐篷的容量不够(self):
        issues = verify(
            _facts(people=3),
            [],
            _itinerary(),
            _gear({"comfort_temp_c": -5}, tent_specs={"capacity_persons": 2}),
            {},
        )
        critical = [issue for issue in issues if issue.severity == "critical"]
        assert any("帐篷" in str(issue.constraint) and "人数" in str(issue.constraint) for issue in critical)

    def test_容量够就不报(self):
        issues = verify(
            _facts(people=2),
            [],
            _itinerary(),
            _gear({"comfort_temp_c": -5}, tent_specs={"capacity_persons": 2}),
            {},
        )
        assert not [i for i in issues if "帐篷" in str(i.constraint) and i.severity == "critical"]

    def test_帐篷没有容量信息标为未知(self):
        issues = verify(
            _facts(people=3),
            [],
            _itinerary(),
            _gear({"comfort_temp_c": -5}, tent_specs={}),
            {},
        )
        assert any("帐篷" in str(i.constraint) and "未知" in str(i.constraint) for i in issues)


class TestCampingCompleteness:
    def test_缺帐篷仍然报齐全性(self):
        gear = [item for item in _gear({"comfort_temp_c": -5}) if item.name != "帐篷"]
        issues = verify(_facts(), [], _itinerary(), gear, {})
        assert any("露营装备" in str(issue.constraint) for issue in issues)

    def test_非露营不查露营装备(self):
        gear = [GearItem(name="T恤", status="owned")]
        facts = _facts(lodging="hotel", min_temp_c=None)
        issues = verify(facts, [], _itinerary(), gear, {})
        assert not [i for i in issues if "露营装备" in str(i.constraint)]
