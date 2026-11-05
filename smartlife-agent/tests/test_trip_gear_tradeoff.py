"""统一代价模块的单测。

这里的断言不是"算得对"，而是把三条设计决定钉死：
判定用归一化幅度、排序用未归一化幅度、每一项都可解释。
以后谁想改口径，先看这些用例为什么这么写。
"""
from __future__ import annotations

import pytest

from app.trip_gear.tradeoff import (
    DEFAULT_CONFIDENCE,
    DEFAULT_TAU,
    HARD_WEIGHT,
    SOFT_WEIGHT,
    CostTerm,
    acceptable,
    budget_delta,
    capped_total,
    constraint_weight,
    explain,
    gear_missing_delta,
    make_sort_term,
    make_term,
    missing_delta,
    parse_tau,
    source_confidence,
    total,
)


class TestSourceConfidence:
    def test_按来源前缀分级(self):
        # 用户原话最可信，模型推断最弱，中间留两档给工具与规则。
        assert source_confidence("用户原话") == 1.0
        assert source_confidence("天气工具") == 0.9
        assert source_confidence("规则推导") == 0.8
        assert source_confidence("模型推断") == 0.5

    def test_带说明的来源也认得出(self):
        # 约束里常写成"规则推导（夜间温度留 5℃ 余量）"，前缀匹配要能命中。
        assert source_confidence("规则推导（夜间温度留 5℃ 余量）") == 0.8

    def test_未知来源回落到默认(self):
        assert source_confidence("") == DEFAULT_CONFIDENCE
        assert source_confidence("天上掉的") == DEFAULT_CONFIDENCE


class TestConstraintWeight:
    def test_硬约束权重等于置信度(self):
        assert constraint_weight(True, "用户原话") == pytest.approx(HARD_WEIGHT * 1.0)

    def test_软约束打折(self):
        assert constraint_weight(False, "用户原话") == pytest.approx(SOFT_WEIGHT * 1.0)

    def test_模型推断的硬约束弱于用户原话的软约束(self):
        # 这一条是权重设计的关键：推断出来的"必须"不该压过用户说的"尽量"。
        assert constraint_weight(True, "模型推断") < constraint_weight(False, "用户原话")


class TestBudgetDelta:
    def test_未超支代价为零(self):
        assert budget_delta(800, 1000) == (0.0, 0.0)

    def test_刚好等于预算不算超(self):
        assert budget_delta(1000, 1000) == (0.0, 0.0)

    def test_超支两成(self):
        capped, raw = budget_delta(1200, 1000)
        assert capped == pytest.approx(0.2)
        assert raw == pytest.approx(0.2)

    def test_归一化幅度封顶而原始幅度不封顶(self):
        # 这是"判定用 capped、排序用 raw"的根据：超支 3 倍与 10 倍
        # 在判定上同样算"严重的超支"，但排序必须分出谁更疼。
        capped3, raw3 = budget_delta(4000, 1000)
        capped10, raw10 = budget_delta(11000, 1000)
        assert capped3 == capped10 == 1.0
        assert raw10 > raw3 > 1.0

    def test_预算非法时不产生零代价(self):
        # 预算为 0 却花了 500：不能因为没法算比例就当作没超支。
        assert budget_delta(500, 0) == (1.0, 500.0)
        assert budget_delta(0, 0) == (0.0, 0.0)


class TestMissingDelta:
    def test_保护项缺了是满幅(self):
        assert missing_delta("帐篷", optional={"防晒霜"}, protected={"帐篷"}) == 1.0

    def test_可选件缺了压低(self):
        assert missing_delta("防晒霜", optional={"防晒霜"}, protected={"帐篷"}) == 0.2

    def test_其余按功能必需(self):
        assert missing_delta("登山鞋", optional={"防晒霜"}, protected={"帐篷"}) == 0.5

    def test_便捷版复用既有名单(self):
        # 名单不另建一套：帐篷在保护名单，防晒霜在可选清单。
        assert gear_missing_delta("帐篷") == 1.0
        assert gear_missing_delta("防晒霜") == 0.2


class TestCostTerms:
    def test_判定项与排序项取不同幅度(self):
        delta = budget_delta(4000, 1000)
        judged = make_term("预算", hard=True, source="用户原话", delta=delta)
        ranked = make_sort_term("预算", hard=True, source="用户原话", delta=delta)
        assert judged.delta == 1.0 and judged.capped is True
        assert ranked.delta == pytest.approx(3.0) and ranked.capped is False
        # 同一件事，判定上等价的两笔超支，排序上不等价。
        assert ranked.cost > judged.cost

    def test_代价按权重乘幅度(self):
        term = CostTerm(label="预算", weight=0.8, delta=0.5)
        assert term.cost == pytest.approx(0.4)

    def test_合计与明细(self):
        terms = [
            CostTerm(label="预算", weight=1.0, delta=0.3),
            CostTerm(label="睡袋", weight=0.8, delta=1.0),
            CostTerm(label="防晒霜", weight=0.48, delta=0.2),
        ]
        assert total(terms) == pytest.approx(0.3 + 0.8 + 0.096)
        rows = explain(terms)
        assert [row["label"] for row in rows] == ["睡袋", "预算", "防晒霜"]
        assert rows[0]["cost"] == pytest.approx(0.8)

    def test_明细可截断(self):
        terms = [CostTerm(label=f"项{i}", weight=1.0, delta=1.0) for i in range(12)]
        assert len(explain(terms, limit=5)) == 5


class TestAcceptable:
    def test_低于门限可自动松弛(self):
        assert acceptable([CostTerm(label="x", weight=1.0, delta=0.1)]) is True

    def test_高于门限要外部介入(self):
        assert acceptable([CostTerm(label="x", weight=1.0, delta=1.0)]) is False

    def test_归一化合计只算判定项(self):
        terms = [
            make_term("预算", hard=True, source="用户原话", delta=budget_delta(2000, 1000)),
            make_sort_term("预算", hard=True, source="用户原话", delta=budget_delta(2000, 1000)),
        ]
        # 排序项不参与"是否可接受"的判断，否则同一笔超支会被算两遍。
        assert capped_total(terms) == pytest.approx(1.0)


class TestTau:
    def test_非法配置回落到默认(self):
        assert parse_tau(None) == DEFAULT_TAU
        assert parse_tau("abc") == DEFAULT_TAU
        assert parse_tau(-1) == DEFAULT_TAU

    def test_字符串数字可用(self):
        assert parse_tau("0.3") == pytest.approx(0.3)
