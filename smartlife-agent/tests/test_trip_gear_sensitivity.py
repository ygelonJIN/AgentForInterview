"""敏感性分析的测试：结论要能复现，扰动要能恢复，τ 不能改可行性。"""
from __future__ import annotations

from app.trip_gear import tradeoff
from app.trip_gear.sensitivity import analyze
from app.trip_gear.verdict import action_cost, plan_repair


def test_分析可重复且不污染常量():
    before = dict(tradeoff.ACTION_COST), tradeoff.SOFT_WEIGHT, tradeoff.SOURCE_CONFIDENCE_PREFIX
    first = analyze()
    after = dict(tradeoff.ACTION_COST), tradeoff.SOFT_WEIGHT, tradeoff.SOURCE_CONFIDENCE_PREFIX
    second = analyze()
    assert first == second
    assert before == after, "扰动必须在退出时恢复，否则后续评测被带偏"


def test_档位正负二十不改变选择():
    report = analyze()
    assert report["action_tier"]["stable_under_pm20"] is True
    assert report["action_tier"]["first_flip_factor"]["skip"] is not None, "压到 0.4 倍应能测出翻转边界"


def test_阈值只改自动性不改结论():
    report = analyze()
    assert report["verdict"]["tau_affects_feasibility"] is False
    for row in report["tau"]["sweep"]:
        assert row["state_changed"] == []
    # 至少有某个 τ 让"自动/确认"发生变化，否则这条扫描没有信息量
    assert any(row["auto_changed"] for row in report["tau"]["sweep"])


def test_动作档位是现取的():
    """改 tradeoff.ACTION_COST 应立刻影响排序，说明没有第二份拷贝。"""
    original = dict(tradeoff.ACTION_COST)
    try:
        tradeoff.ACTION_COST = {**original, "downgrade": 0.9}
        assert action_cost({"kind": "downgrade"}) == 0.9
        plan = plan_repair(
            [
                {"kind": "downgrade", "name": "睡袋", "saving": 400},
                {"kind": "skip", "name": "营地灯", "saving": 400},
            ],
            300,
        )
        assert plan is not None
        assert plan[1][0]["kind"] == "skip", "降档被抬到 0.9 后应先选省略"
    finally:
        tradeoff.ACTION_COST = original


def test_软权重影响违反代价():
    report = analyze()
    changed = [row for row in report["soft_weight"]["perturbations"] if row["cost_changed"]]
    assert changed, "软约束权重变化至少要影响某个案例的违反代价"
