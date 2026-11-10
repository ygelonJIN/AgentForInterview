"""打分选品的单测：不许退回"只挑便宜"，也不许为"越大越好"买单。

这些用例都对应真实缺陷：按价格排序会让 10℃ 的夜选到刚卡线的 5℃ 棉睡袋；
按候选内 min-max 归一化评分会让一堆 4.8 里的 4.8 变成 0 分；
把"容量越大越好"当真，两人出行会被推四人帐篷。
"""
from __future__ import annotations

import pytest

from app.trip_gear.models import GearItem, TripFacts
from app.trip_gear.select import rank
from app.trip_gear.spec_schema import quality_score, ranges_from, subcategory_for


def _facts(min_temp=10, people=2) -> TripFacts:
    return TripFacts(destination="莫干山", days=2, people=people, lodging="camp", min_temp_c=min_temp)


def _bag(name, price, rating=4.6, temp=5, power=None, weight=800, stock=10):
    specs = {"comfort_temp_c": temp, "weight_g": weight}
    if power:
        specs["fill_power"] = power
        specs["fill_weight_g"] = 300
        specs["down_ratio"] = 0.9
    return {"name": name, "price": price, "rating": rating, "stock": stock, "specs": specs}


class TestFit:
    def test_硬伤口径直接出局(self):
        # 15℃ 睡袋去 10℃ 的夜里（要求 ≤5℃）：不该出现在候选里。
        ranked = rank([_bag("棉睡袋", 120, temp=15), _bag("羽绒睡袋", 300, temp=-5, power=700)],
                      "睡袋", "睡袋", _facts(), remaining=1000, qty=1)
        assert [item.product["name"] for item in ranked] == ["羽绒睡袋"]

    def test_余量越大分越高(self):
        ranked = rank([_bag("卡线 5℃", 200, temp=5), _bag("从容 -5℃", 280, temp=-5, power=700)],
                      "睡袋", "睡袋", _facts(), remaining=1000, qty=1)
        assert ranked[0].product["name"] == "从容 -5℃"
        assert ranked[0].fit > ranked[1].fit

    def test_规格未知只给一半分(self):
        unknown = {"name": "没写温标", "price": 150, "rating": 4.6, "stock": 5, "specs": {}}
        ranked = rank([unknown, _bag("卡线 5℃", 200, temp=5)], "睡袋", "睡袋", _facts(), remaining=1000)
        assert ranked[0].product["name"] == "卡线 5℃"


class TestRating:
    def test_评分按固定刻度而不是候选内比较(self):
        # 全是 4.8 的候选里，4.8 不该被算成 0 分。
        a = _bag("A", 200, rating=4.8, temp=-5, power=700)
        b = _bag("B", 200, rating=4.8, temp=-5, power=700)
        ranked = rank([a, b], "睡袋", "睡袋", _facts(), remaining=1000)
        assert ranked[0].rating_norm == pytest.approx(0.8667, abs=1e-3)


class TestOvershoot:
    def test_两人出行不该被推四人帐篷(self):
        two = {"name": "双人帐篷", "price": 599, "rating": 4.6, "stock": 5,
               "specs": {"capacity_persons": 2, "packed_weight_g": 2400}}
        four = {"name": "四人帐篷", "price": 1899, "rating": 4.6, "stock": 5,
                "specs": {"capacity_persons": 4, "packed_weight_g": 4200}}
        ranked = rank([two, four], "帐篷", "帐篷", _facts(people=2), remaining=3000)
        assert ranked[0].product["name"] == "双人帐篷"

    def test_坐不下的一律出局(self):
        two = {"name": "双人帐篷", "price": 599, "rating": 4.6, "stock": 5,
               "specs": {"capacity_persons": 2}}
        ranked = rank([two], "帐篷", "帐篷", _facts(people=4), remaining=3000)
        assert ranked == []


class TestBudget:
    def test_超出剩余额度不选(self):
        bag = _bag("贵睡袋", 900, temp=-5, power=700)
        assert rank([bag], "睡袋", "睡袋", _facts(), remaining=500, qty=1) == []

    def test_数量参与预算判断(self):
        bag = _bag("睡袋", 300, temp=-5, power=700)
        assert rank([bag], "睡袋", "睡袋", _facts(), remaining=500, qty=2) == []
        assert rank([bag], "睡袋", "睡袋", _facts(), remaining=700, qty=2)

    def test_无货不参与(self):
        assert rank([_bag("缺货", 200, temp=-5, power=700, stock=0)], "睡袋", "睡袋", _facts(), 1000) == []


class TestPreference:
    def test_品牌偏好只是加分不是决定(self):
        a = _bag("牧高笛款", 300, temp=-5, power=700)
        a["brand"] = "牧高笛"
        b = _bag("更便宜款", 260, temp=-5, power=700)
        b["brand"] = "其他"
        ranked = rank([a, b], "睡袋", "睡袋", _facts(), 1000, preferred_brands=["牧高笛"])
        assert ranked[0].product["brand"] == "牧高笛"

        ranked_without = rank([a, b], "睡袋", "睡袋", _facts(), 1000)
        assert ranked_without[0].product["brand"] == "其他"


class TestSpecSchema:
    def test_品类参数表按品类分开(self):
        # 羽绒服有蓬松度，睡袋没有这个"越大越好"以外的同名字段语义，两者共用同一套打分会互相污染。
        from app.trip_gear.spec_schema import schema_for

        assert "fill_power" in schema_for("羽绒服")
        assert "fill_power" in schema_for("睡袋")
        assert "cpu_score" in schema_for("手机")
        assert schema_for("帐篷") and "cpu_score" not in schema_for("帐篷")
        assert subcategory_for("羽绒服") == "服装"
        assert subcategory_for("帐篷") == "露营"

    def test_低值更好的字段方向正确(self):
        ranges = {"comfort_temp_c": (-20, 15)}
        colder = quality_score("睡袋", {"comfort_temp_c": -20}, ranges)
        warmer = quality_score("睡袋", {"comfort_temp_c": 15}, ranges)
        assert colder > warmer

    def test_候选区间计算(self):
        rows = [{"specs": {"weight_g": 300}}, {"specs": {"weight_g": 900}}]
        assert ranges_from(rows, "睡袋")["weight_g"] == (300, 900)


class TestReviewTieBreak:
    """评价只做平局裁决：分差够大时一次都不查，避免口碑稀释参数匹配。"""

    def _twin(self, price=500):
        return [
            {"id": "1", "name": "帐篷A", "price": price, "rating": 4.5, "stock": 5},
            {"id": "2", "name": "帐篷B", "price": price, "rating": 4.5, "stock": 5},
        ]

    def test_平局由真实评价裁决(self):
        from app.trip_gear.select import pick

        stats = {"1": {"rating": 3.8, "count": 30}, "2": {"rating": 4.7, "count": 12}}
        best = pick(self._twin(), "帐篷", "帐篷", review_lookup=lambda pid: stats[pid])

        assert best.product["name"] == "帐篷B"
        assert "口碑平局裁决" in best.reason

    def test_没有评价时保持打分结果(self):
        from app.trip_gear.select import pick

        best = pick(self._twin(), "帐篷", "帐篷", review_lookup=lambda _pid: {})
        assert best.product["name"] == "帐篷A"

    def test_分差够大时不查评价(self):
        from app.trip_gear.select import pick

        calls = []
        candidates = [
            {"id": "1", "name": "帐篷A", "price": 300, "rating": 4.8, "stock": 5},
            {"id": "2", "name": "帐篷B", "price": 900, "rating": 3.6, "stock": 5},
        ]
        best = pick(
            candidates, "帐篷", "帐篷",
            review_lookup=lambda pid: calls.append(pid) or {"rating": 1.0, "count": 9},
        )

        assert calls == []
        assert best.product["name"] == "帐篷A"
