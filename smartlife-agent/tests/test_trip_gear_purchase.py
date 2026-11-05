"""采购可履约的单测：买得到、来得及。

时间一律显式传入（``today``），不依赖系统时钟——否则这套测试明天就会红。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.trip_gear import purchase
from app.trip_gear.models import GearItem, TripFacts
from app.trip_gear.purchase import (
    STATUS_LATE,
    STATUS_OK,
    STATUS_OUT_OF_STOCK,
    STATUS_UNKNOWN,
    check_purchases,
    delivery_check,
    days_until,
    departure_date,
)

TODAY = date(2026, 11, 5)


class TestDates:
    def test_解析ISO日期(self):
        facts = TripFacts(start_date="2026-11-08")
        assert departure_date(facts) == date(2026, 11, 8)

    def test_认不出的日期返回空(self):
        assert departure_date(TripFacts(start_date="下周五")) is None
        assert departure_date(TripFacts()) is None

    def test_距出发天数(self):
        assert days_until(date(2026, 11, 8), TODAY) == 3
        assert days_until(date(2026, 11, 5), TODAY) == 0

    def test_已出发返回空而不是负数(self):
        # 负天数会被下游当成"还来得及"，这里直接判为无法判断。
        assert days_until(date(2026, 11, 1), TODAY) is None


class TestDeliveryCheck:
    def test_来得及(self):
        result = delivery_check("帐篷", {"name": "双人帐篷", "stock": 10, "lead_time_days": 2}, 3)
        assert result.status == STATUS_OK

    def test_刚好同一天算来得及(self):
        result = delivery_check("睡袋", {"name": "睡袋", "stock": 5, "lead_time_days": 3}, 3)
        assert result.status == STATUS_OK

    def test_到货比出发晚就是赶不上(self):
        result = delivery_check("帐篷", {"name": "双人帐篷", "stock": 10, "lead_time_days": 5}, 2)
        assert result.status == STATUS_LATE
        assert "5 天" in result.hint and "2 天" in result.hint

    def test_无货优先于时效(self):
        # 没货就没有"到货时间"这回事，结论要报无货，不能报"来得及"。
        result = delivery_check("帐篷", {"name": "双人帐篷", "stock": 0, "lead_time_days": 1}, 9)
        assert result.status == STATUS_OUT_OF_STOCK

    def test_缺时效不算通过(self):
        result = delivery_check("帐篷", {"name": "双人帐篷", "stock": 10}, 3)
        assert result.status == STATUS_UNKNOWN

    def test_缺出发日期不算通过(self):
        result = delivery_check("帐篷", {"name": "双人帐篷", "stock": 10, "lead_time_days": 1}, None)
        assert result.status == STATUS_UNKNOWN


class TestCheckPurchases:
    def _gear(self):
        return [
            GearItem(name="帐篷", status="need_buy"),
            GearItem(name="睡袋", status="owned"),
            GearItem(name="头灯", status="need_buy"),
        ]

    def test_只看待购件(self):
        products = {"帐篷": {"name": "帐篷", "stock": 5, "lead_time_days": 1}}
        results = check_purchases(self._gear(), products, TripFacts(start_date="2026-11-08"), today=TODAY)
        assert [result.gear for result in results] == ["帐篷"]

    def test_没匹配到商品不重复报(self):
        # "库未覆盖"由校验器另一条负责，这里必须跳过，否则同一件事报两遍。
        results = check_purchases(self._gear(), {}, TripFacts(start_date="2026-11-08"), today=TODAY)
        assert results == []

    def test_缺出发日期时如实说不知道(self):
        products = {"帐篷": {"name": "帐篷", "stock": 5, "lead_time_days": 1}}
        results = check_purchases(self._gear(), products, TripFacts(), today=TODAY)
        assert results[0].status == STATUS_UNKNOWN


class TestVerifyIntegration:
    def test_赶不上进入校验结果(self):
        from app.trip_gear.models import DayPlan, Itinerary, ItineraryItem
        from app.trip_gear.verify import verify

        facts = TripFacts(destination="杭州", days=1, people=2, start_date="2026-11-07")
        itinerary = Itinerary(
            destination="杭州",
            days=[DayPlan(day=1, items=[ItineraryItem(time="10:00", activity="出发", duration_min=60, cost=100)])],
        )
        gear = [GearItem(name="帐篷", status="need_buy")]
        products = {"帐篷": {"name": "双人防水帐篷", "price": 599, "stock": 60, "lead_time_days": 5}}
        issues = verify(facts, [], itinerary, gear, products, today=TODAY)
        critical = [issue for issue in issues if issue.severity == "critical"]
        assert any("赶不上" in str(issue.constraint) for issue in critical)

    def test_来得及不报(self):
        from app.trip_gear.models import DayPlan, Itinerary, ItineraryItem
        from app.trip_gear.verify import verify

        facts = TripFacts(destination="杭州", days=1, people=2, start_date="2026-11-20")
        itinerary = Itinerary(
            destination="杭州",
            days=[DayPlan(day=1, items=[ItineraryItem(time="10:00", activity="出发", duration_min=60, cost=100)])],
        )
        gear = [GearItem(name="帐篷", status="need_buy")]
        products = {"帐篷": {"name": "双人防水帐篷", "price": 599, "stock": 60, "lead_time_days": 5}}
        issues = verify(facts, [], itinerary, gear, products, today=TODAY)
        assert not [i for i in issues if "赶不上" in str(i.constraint)]


def _finder(rows):
    """假检索回调：同时接受 subcategory= 与 query= 两种调用。"""
    def _call(**kwargs):
        return list(rows)
    return _call


class TestSubstitutes:
    CURRENT = {"name": "双人防水帐篷", "price": 599, "subcategory": "露营", "stock": 60, "lead_time_days": 5}

    def test_只留赶得上的款(self):
        rows = [
            {"name": "速开帐篷", "price": 459, "stock": 20, "lead_time_days": 2},
            {"name": "大帐篷", "price": 399, "stock": 20, "lead_time_days": 8},
        ]
        subs = purchase.find_substitutes(self.CURRENT, 3, _finder(rows))
        assert [s.name for s in subs] == ["速开帐篷"]

    def test_排除当前这款(self):
        rows = [{"name": "双人防水帐篷", "price": 599, "stock": 60, "lead_time_days": 1}]
        assert purchase.find_substitutes(self.CURRENT, 3, _finder(rows)) == []

    def test_无货的款不推荐(self):
        rows = [{"name": "速开帐篷", "price": 459, "stock": 0, "lead_time_days": 1}]
        assert purchase.find_substitutes(self.CURRENT, 3, _finder(rows)) == []

    def test_按价格升序并给出差价(self):
        rows = [
            {"name": "贵的", "price": 799, "stock": 5, "lead_time_days": 1},
            {"name": "便宜的", "price": 459, "stock": 5, "lead_time_days": 2},
        ]
        subs = purchase.find_substitutes(self.CURRENT, 3, _finder(rows))
        assert [s.name for s in subs] == ["便宜的", "贵的"]
        assert subs[0].delta_price == pytest.approx(-140.0)
        assert subs[1].delta_price == pytest.approx(200.0)

    def test_没有检索能力时返回空而不是报错(self):
        assert purchase.find_substitutes(self.CURRENT, 3, None) == []

    def test_不知道还剩几天就不推荐(self):
        rows = [{"name": "速开帐篷", "price": 459, "stock": 5, "lead_time_days": 1}]
        assert purchase.find_substitutes(self.CURRENT, None, _finder(rows)) == []

    def test_同品类搜不到时退回按名称检索(self):
        calls = []

        def _call(**kwargs):
            calls.append(kwargs)
            return [{"name": "速开帐篷", "price": 459, "stock": 5, "lead_time_days": 1}]

        product = dict(self.CURRENT, subcategory="")
        subs = purchase.find_substitutes(product, 3, _call)
        assert subs and calls and "query" in calls[0]

    def test_自提只看有没有货(self):
        assert purchase.pickup_available({"stock": 3}) is True
        assert purchase.pickup_available({"stock": 0}) is False
        assert purchase.pickup_available({}) is False


class TestVerifyAlternatives:
    def _case(self, product, find_alternatives=None):
        from app.trip_gear.models import DayPlan, Itinerary, ItineraryItem
        from app.trip_gear.verify import verify

        facts = TripFacts(destination="杭州", days=1, people=2, start_date="2026-11-07")
        itinerary = Itinerary(
            destination="杭州",
            days=[DayPlan(day=1, items=[ItineraryItem(time="10:00", activity="出发", duration_min=60, cost=100)])],
        )
        gear = [GearItem(name="帐篷", status="need_buy")]
        return verify(facts, [], itinerary, gear, {"帐篷": product}, today=TODAY, find_alternatives=find_alternatives)

    def test_赶不上时给出替代款(self):
        rows = [{"name": "速开帐篷", "price": 459, "stock": 5, "lead_time_days": 1, "rating": 4.6}]
        issues = self._case(
            {"name": "双人防水帐篷", "price": 599, "subcategory": "露营", "stock": 60, "lead_time_days": 5},
            _finder(rows),
        )
        target = next(issue for issue in issues if "赶不上" in str(issue.constraint))
        assert target.alternatives and target.alternatives[0]["name"] == "速开帐篷"
        assert "可换" in target.hint and "-140" in target.hint

    def test_没有替代款时说明白(self):
        issues = self._case(
            {"name": "双人防水帐篷", "price": 599, "subcategory": "露营", "stock": 60, "lead_time_days": 5},
            _finder([]),
        )
        target = next(issue for issue in issues if "赶不上" in str(issue.constraint))
        assert target.alternatives == []
        assert "没有能赶在出发前到货的款" in target.hint
        # 有货 -> 至少还能到店自提。
        assert "自提" in target.hint

    def test_无货时给替代款(self):
        rows = [{"name": "速开帐篷", "price": 459, "stock": 5, "lead_time_days": 1}]
        issues = self._case(
            {"name": "双人防水帐篷", "price": 599, "subcategory": "露营", "stock": 0, "lead_time_days": 1},
            _finder(rows),
        )
        target = next(issue for issue in issues if "无货" in str(issue.constraint))
        assert target.alternatives[0]["name"] == "速开帐篷"
