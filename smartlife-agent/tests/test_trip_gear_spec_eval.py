"""评测模块与场景集的测试。

原则：评测本身也要被测。``new_accuracy`` 如果是加分函数写错，结论会漂亮
但没意义——这里的断言就是防止"评测器自己说谎"。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.trip_gear.spec_eval import (
    EXPECTATIONS,
    build_case,
    evaluate,
    legacy_bag_verdict,
    load_scenarios,
    summarize,
)

SCENARIOS = Path(__file__).resolve().parents[1] / "data" / "evaluation" / "trip_gear.spec.jsonl"
DB = Path(__file__).resolve().parents[1] / "data" / "products.db"


def _write(base: Path, lines: list) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = base / "scenarios.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestLoadScenarios:
    def test_读取内置场景集(self):
        scenarios = load_scenarios(SCENARIOS)
        assert len(scenarios) >= 8
        assert {item["id"] for item in scenarios} >= {"camp-cold-weak-bag", "hotel-no-bag-needed"}
        for item in scenarios:
            assert item["expect"] in EXPECTATIONS

    def test_期望值写错直接报错(self, tmp_path):
        good = _write(tmp_path / "a", ['{"id": "x", "expect": "ok"}'])
        assert load_scenarios(good)[0]["expect"] == "ok"
        bad = _write(tmp_path / "b", ['{"id": "x", "expect": "很好"}'])
        with pytest.raises(ValueError):
            load_scenarios(bad)

    def test_非法JSON不静默跳过(self, tmp_path):
        path = _write(tmp_path, ["{不是json"])
        with pytest.raises(ValueError):
            load_scenarios(path)

    def test_缺少id报错(self, tmp_path):
        path = _write(tmp_path, ['{"expect": "ok"}'])
        with pytest.raises(ValueError):
            load_scenarios(path)

class TestLegacyVerdict:
    def test_旧逻辑永远判不出不够用(self):
        from app.trip_gear.models import GearItem

        # 10℃ 睡袋去 3℃ 的夜里：旧逻辑只看理由里有没有"温标"。
        bag = GearItem(name="睡袋", reason="露营过夜，温标 ≤ -2℃", specs={"comfort_temp_c": 10})
        assert legacy_bag_verdict([bag]) == "pass"

    def test_旧逻辑的三种输出(self):
        from app.trip_gear.models import GearItem

        assert legacy_bag_verdict([]) == "missing"
        assert legacy_bag_verdict([GearItem(name="睡袋")]) == "unknown"
        assert legacy_bag_verdict([GearItem(name="睡袋", reason="温标")]) == "pass"

    def test_没有任何输入能得到insufficient(self):
        from app.trip_gear.models import GearItem

        cases = [[], [GearItem(name="睡袋")], [GearItem(name="睡袋", reason="温标 ≤ -2℃")]]
        for gear in cases:
            assert legacy_bag_verdict(gear) != "insufficient"


class TestBuildCase:
    def test_已有规格能带到装备清单(self):
        case = build_case(
            {
                "id": "x",
                "facts": {"destination": "杭州", "days": 2, "people": 2, "lodging": "camp", "min_temp_c": 3},
                "owned": [{"name": "睡袋", "qty": 2, "specs": {"comfort_temp_c": 10}}],
                "products": {},
            }
        )
        bag = next(item for item in case["gear"] if item.name == "睡袋")
        assert bag.status == "owned"
        assert bag.specs["comfort_temp_c"] == 10

    def test_数量不足时规格也不能丢(self):
        case = build_case(
            {
                "id": "x",
                "facts": {"destination": "杭州", "days": 2, "people": 2, "lodging": "camp", "min_temp_c": 3},
                "owned": [{"name": "睡袋", "qty": 1, "specs": {"comfort_temp_c": 10}}],
                "products": {},
            }
        )
        bag = next(item for item in case["gear"] if item.name == "睡袋")
        assert bag.status == "need_buy"
        assert bag.specs["comfort_temp_c"] == 10

    def test_住酒店不产生温标约束(self):
        case = build_case(
            {
                "id": "x",
                "facts": {"destination": "上海", "days": 3, "people": 1, "lodging": "hotel", "min_temp_c": 5},
                "owned": [],
                "products": {},
            }
        )
        assert case["constraints"] == []


class TestEvaluate:
    def test_内置场景集上改后全对(self):
        report = evaluate(load_scenarios(SCENARIOS))
        assert report["summary"]["new_accuracy"] == 1.0
        assert report["summary"]["new_missed_insufficient"] == []

    def test_旧逻辑准确率明显更低(self):
        summary = evaluate(load_scenarios(SCENARIOS))["summary"]
        assert summary["legacy_accuracy"] < 0.6
        assert summary["legacy_reported_insufficient"] == 0
        assert len(summary["legacy_false_accept"]) >= 3

    def test_分布与场景数一致(self):
        summary = evaluate(load_scenarios(SCENARIOS))["summary"]
        assert sum(summary["new_distribution"].values()) == summary["scenarios"]

    def test_空场景集不除零(self):
        summary = summarize([])
        assert summary["new_accuracy"] == 0.0
        assert summary["spec_unknown_rate"] == 0.0


class TestProductSpecsInDb:
    """商品库真的带上规格了吗——迁移没跑或没跑干净，这里会红。"""

    def test_睡袋规格已落库(self):
        if not DB.exists():
            pytest.skip("products.db 不存在")
        from app.retrieval.repositories import ProductRepository

        # 库里睡袋有几十款，按名称精确检索那一款，别按排序猜。
        rows = ProductRepository(str(DB)).search(query="四季保暖睡袋", limit=5)
        assert rows, "商品库里搜不到四季保暖睡袋"
        target = next(row for row in rows if row["name"] == "四季保暖睡袋")
        specs = json.loads(target["specs"])
        assert specs["comfort_temp_c"] == -5

    def test_查询结果包含时效字段(self):
        if not DB.exists():
            pytest.skip("products.db 不存在")
        from app.retrieval.repositories import ProductRepository

        rows = ProductRepository(str(DB)).search(query="帐篷", limit=1)
        assert rows and "lead_time_days" in rows[0]
