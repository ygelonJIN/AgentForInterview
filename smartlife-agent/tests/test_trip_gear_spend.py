"""预算利用的单测：钱要花掉，但不能乱花。

四条边界各一条用例：只用库里的、不超预算、不降级保障、不碰用户明说的条件。
"""
from __future__ import annotations

import pytest

from app.trip_gear.models import GearItem, TripFacts
from app.trip_gear.spend import MIN_UPGRADE_DELTA, upgrade_within_budget

CURRENT = {"name": "双人防水帐篷", "price": 599, "stock": 5, "specs": {"capacity_persons": 2}}


def _finder(rows):
    return lambda **_: list(rows)


def _gear(name="帐篷", qty=1):
    return [GearItem(name=name, qty=qty, status="need_buy")]


class TestSpend:
    def test_有更贵的可用款就换掉(self):
        rows = [{"name": "三人自动帐篷", "price": 1299, "stock": 5, "specs": {"capacity_persons": 3}}]
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(destination="杭州", days=2, people=2, lodging="camp"),
            _gear(), products, remaining=2000, find_alternatives=_finder(rows),
        )
        assert [item.gear for item in result.upgrades] == ["帐篷"]
        assert products["帐篷"]["price"] == 1299
        assert result.spent == pytest.approx(700.0)

    def test_不超预算(self):
        rows = [{"name": "三人自动帐篷", "price": 1299, "stock": 5, "specs": {"capacity_persons": 3}}]
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(days=2, people=2, lodging="camp"),
            _gear(), products, remaining=100, find_alternatives=_finder(rows),
        )
        assert result.upgrades == []
        assert products["帐篷"]["price"] == 599

    def test_不会换成规格不够的款(self):
        # 3 人出行，换一个只有 2 人容量的"更贵"款是降级，不能要。
        rows = [{"name": "贵但小", "price": 999, "stock": 5, "specs": {"capacity_persons": 2}}]
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(days=2, people=3, lodging="camp"),
            _gear(), products, remaining=2000, find_alternatives=_finder(rows),
        )
        assert result.upgrades == []

    def test_规格未知的候选也不要(self):
        rows = [{"name": "无规格大帐篷", "price": 999, "stock": 5}]
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(days=2, people=2, lodging="camp"),
            _gear(), products, remaining=2000, find_alternatives=_finder(rows),
        )
        assert result.upgrades == []

    def test_无货的不换(self):
        rows = [{"name": "三人自动帐篷", "price": 1299, "stock": 0, "specs": {"capacity_persons": 3}}]
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(days=2, people=2, lodging="camp"),
            _gear(), products, remaining=2000, find_alternatives=_finder(rows),
        )
        assert result.upgrades == []

    def test_到货赶不上就不换(self):
        rows = [{
            "name": "三人自动帐篷", "price": 1299, "stock": 5,
            "specs": {"capacity_persons": 3}, "lead_time_days": 9,
        }]
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(days=2, people=2, lodging="camp", start_date="2026-11-07"),
            _gear(), products, remaining=2000, find_alternatives=_finder(rows),
        )
        assert result.upgrades == []

    def test_差价太小就不折腾(self):
        rows = [{"name": "双人防水帐篷 Pro", "price": 620, "stock": 5, "specs": {"capacity_persons": 2}}]
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(days=2, people=2, lodging="camp"),
            _gear(), products, remaining=2000, find_alternatives=_finder(rows),
        )
        assert result.upgrades == []
        assert MIN_UPGRADE_DELTA > 20

    def test_钱花不掉要说清原因(self):
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(days=2, people=2, lodging="camp"),
            _gear(), products, remaining=800, find_alternatives=_finder([]),
        )
        assert result.reason
        assert "没有更贵" in result.reason

    def test_没有检索能力时不升级(self):
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(days=2, people=2, lodging="camp"), _gear(), products, 2000, None
        )
        assert result.upgrades == []
        assert "检索能力" in result.reason

    def test_升级后记录来源(self):
        rows = [{"name": "三人自动帐篷", "price": 1299, "stock": 5, "specs": {"capacity_persons": 3}}]
        products = {"帐篷": dict(CURRENT)}
        upgrade_within_budget(
            TripFacts(days=2, people=2, lodging="camp"),
            _gear(), products, remaining=2000, find_alternatives=_finder(rows),
        )
        assert products["帐篷"]["match"]["upgraded_from"] == "双人防水帐篷"

    def test_已拥有的不参与升级(self):
        rows = [{"name": "三人自动帐篷", "price": 1299, "stock": 5, "specs": {"capacity_persons": 3}}]
        gear = [GearItem(name="帐篷", status="owned")]
        products = {"帐篷": dict(CURRENT)}
        result = upgrade_within_budget(
            TripFacts(days=2, people=2, lodging="camp"), gear, products, 2000, _finder(rows)
        )
        assert result.upgrades == []
