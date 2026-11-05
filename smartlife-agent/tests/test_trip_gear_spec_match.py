"""规格匹配的单测：从"有没有"到"够不够用"。

核心断言只有一句：15 温标的睡袋去 3℃ 的夜里，必须判不够。
这条以前是"理由里有没有'温标'两个字"，等于没校验。
"""
from __future__ import annotations

import pytest

from app.trip_gear.models import GearItem, TripFacts
from app.trip_gear.spec_match import (
    R_VALUE_COLD,
    STATUS_INSUFFICIENT,
    STATUS_OK,
    STATUS_UNKNOWN,
    SpecRequirement,
    check,
    evaluate,
    parse_specs,
    resolve_specs,
    spec_requirements,
)


class TestParseSpecs:
    def test_睡袋温标(self):
        assert parse_specs("舒适温度-5°C 的睡袋", "睡袋")["comfort_temp_c"] == -5

    def test_零下说法(self):
        assert parse_specs("零下5度的睡袋")["comfort_temp_c"] == -5

    def test_没有装备语义的温度不算规格(self):
        # "去 3 度的地方"是目的地描述，不是睡袋温标；误抽会让校验给出错结论。
        assert "comfort_temp_c" not in parse_specs("去 3 度的地方玩")

    def test_帐篷人数(self):
        assert parse_specs("双人防水帐篷")["capacity_persons"] == 2

    def test_背包容量(self):
        assert parse_specs("户外登山背包40L")["volume_l"] == 40

    def test_毫升换算成升(self):
        assert parse_specs("500ml 水杯")["volume_l"] == 0.5

    def test_防水毫米(self):
        assert parse_specs("20000mm 防水")["waterproof_mm"] == 20000

    def test_空文本(self):
        assert parse_specs("") == {}


class TestRequirements:
    def test_露营按夜间温度给温标要求(self):
        facts = TripFacts(destination="杭州", days=2, people=2, lodging="camp", min_temp_c=3)
        reqs = {req.gear: req for req in spec_requirements(facts, {"睡袋", "帐篷", "防潮垫"})}
        # 3℃ 留 5℃ 余量 -> 温标不高于 -2℃
        assert reqs["睡袋"].value == -2
        assert reqs["睡袋"].op == "<="
        assert reqs["帐篷"].value == 2
        assert reqs["帐篷"].op == ">="
        assert reqs["防潮垫"].value == R_VALUE_COLD

    def test_只为清单里有的装备要规格(self):
        facts = TripFacts(days=2, people=2, lodging="camp", min_temp_c=3)
        reqs = spec_requirements(facts, {"睡袋"})
        assert [req.gear for req in reqs] == ["睡袋"]

    def test_没有温度就不编温标要求(self):
        facts = TripFacts(days=2, people=2, lodging="camp")
        reqs = spec_requirements(facts, {"睡袋"})
        assert reqs == []


class TestCheck:
    def _req(self, **kwargs):
        base = dict(gear="睡袋", key="comfort_temp_c", op="<=", value=-2)
        base.update(kwargs)
        return SpecRequirement(**base)

    def test_温标不够(self):
        result = check(self._req(), {"comfort_temp_c": 10})
        assert result.status == STATUS_INSUFFICIENT
        assert result.actual == 10

    def test_温标刚好达标(self):
        assert check(self._req(), {"comfort_temp_c": -2}).status == STATUS_OK

    def test_温标更强也达标(self):
        assert check(self._req(), {"comfort_temp_c": -10}).status == STATUS_OK

    def test_没有规格是未知而不是通过(self):
        # 这条是关键：缺信息不能被当成满足，否则又会退回到"有就算过"。
        assert check(self._req(), {}).status == STATUS_UNKNOWN
        assert check(self._req(), {"comfort_temp_c": None}).status == STATUS_UNKNOWN
        assert check(self._req(), {"comfort_temp_c": "不知道"}).status == STATUS_UNKNOWN

    def test_越大越好的方向(self):
        req = SpecRequirement(gear="帐篷", key="capacity_persons", op=">=", value=3)
        assert check(req, {"capacity_persons": 2}).status == STATUS_INSUFFICIENT
        assert check(req, {"capacity_persons": 3}).status == STATUS_OK


class TestResolveSpecs:
    def test_待购看商品(self):
        item = GearItem(name="睡袋", status="need_buy")
        product = {"specs": {"comfort_temp_c": -5}}
        assert resolve_specs(item, product)["comfort_temp_c"] == -5

    def test_已有看自身(self):
        item = GearItem(name="睡袋", status="owned", specs={"comfort_temp_c": 10})
        assert resolve_specs(item, None)["comfort_temp_c"] == 10

    def test_商品行里的规格是JSON字符串(self):
        # SQLite 里 specs 是 TEXT，取出来是字符串，不解析就等于没有规格。
        item = GearItem(name="睡袋", status="need_buy")
        product = {"specs": '{"comfort_temp_c": -5}'}
        assert resolve_specs(item, product)["comfort_temp_c"] == -5

    def test_自身规格优先(self):
        item = GearItem(name="睡袋", status="owned", specs={"comfort_temp_c": 8})
        product = {"specs": {"comfort_temp_c": -5}}
        assert resolve_specs(item, product)["comfort_temp_c"] == 8


class TestEvaluate:
    def test_只为清单里存在的装备核对(self):
        facts = TripFacts(days=2, people=2, lodging="camp", min_temp_c=3)
        gear = [GearItem(name="睡袋", status="owned", specs={"comfort_temp_c": 10})]
        results = evaluate(gear, {}, facts=facts)
        assert [result.requirement.gear for result in results] == ["睡袋"]
        assert results[0].status == STATUS_INSUFFICIENT

    def test_跳过的装备不参与核对(self):
        facts = TripFacts(days=2, people=2, lodging="camp", min_temp_c=3)
        gear = [GearItem(name="睡袋", status="skipped", specs={"comfort_temp_c": 10})]
        assert evaluate(gear, {}, facts=facts) == []
