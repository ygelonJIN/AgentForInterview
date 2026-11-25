"""校验结论的单测：从"哪里不对"到"那怎么办"。

重点不在四态各自能算出来，而在三件事：
改得动的要说改什么、改完够不够；改不动的要区分"缺钱"和"缺信息"；
实在不行要给出放弃清单，而不是一句"未满足约束"。
"""
from __future__ import annotations

import pytest

from app.trip_gear.models import Constraint, Issue, Verdict
from app.trip_gear.verdict import (
    action_cost,
    budget_deficit,
    gaps,
    judge,
    plan_repair,
)


def _budget_issue(actual: float, limit: float) -> Issue:
    return Issue(constraint="预算：1500 元", actual=actual, limit=limit, severity="critical")


def _option(kind: str, name: str, saving: float) -> dict:
    return {"kind": kind, "name": name, "saving": saving}


class TestBudgetDeficit:
    def test_缺口按实际减预算(self):
        assert budget_deficit([_budget_issue(1800, 1500)]) == 300

    def test_多笔预算不累加(self):
        # 同一笔钱只缺一次；累加会把缺口算大，导致多砍装备。
        issues = [_budget_issue(1800, 1500), _budget_issue(1700, 1500)]
        assert budget_deficit(issues) == 300

    def test_非预算类不计入(self):
        assert budget_deficit([Issue(constraint="睡袋温标 ≤ -2℃", actual=10, limit=-2)]) == 0

    def test_文案里带非数字不会崩(self):
        assert budget_deficit([Issue(constraint="预算", actual="超了", limit=1500)]) == 0


class TestPlanRepair:
    def test_省得够就出方案(self):
        plan = plan_repair([_option("skip", "营地灯", 200), _option("skip", "帽子", 150)], 300)
        assert plan is not None
        cost, chosen = plan
        assert cost == pytest.approx(0.4)
        assert len(chosen) == 2

    def test_省不够就不硬凑(self):
        assert plan_repair([_option("skip", "营地灯", 100)], 300) is None

    def test_优先便宜的改动(self):
        # 降档代价低于省略，所以在都能补上缺口时先换便宜款。
        plan = plan_repair(
            [_option("skip", "营地灯", 400), _option("downgrade", "睡袋", 400)],
            300,
        )
        assert plan is not None
        assert plan[1][0]["kind"] == "downgrade"

    def test_没有缺口就不用改(self):
        assert plan_repair([], 0) == (0.0, [])

    def test_动作代价有档位(self):
        assert action_cost(_option("downgrade", "x", 1)) < action_cost(_option("skip", "x", 1))


class TestJudge:
    def test_没有严重问题就是可行(self):
        verdict = judge([], [Issue(constraint="装备：头灯", severity="minor")])
        assert verdict.state == "feasible"
        assert verdict.auto_applicable is True

    def test_修得动就是可修(self):
        verdict = judge(
            [],
            [_budget_issue(1800, 1500)],
            repair_options=[_option("skip", "营地灯", 200), _option("downgrade", "睡袋", 150)],
        )
        assert verdict.state == "repairable"
        assert verdict.best_cost > 0
        assert "预算：1500 元" in verdict.blocking
        assert verdict.alternatives

    def test_代价高于门限时不自动改(self):
        # 两项省略各 0.2，合计 0.4，超过默认 τ=0.15：能修，但要用户点头。
        verdict = judge(
            [], [_budget_issue(1800, 1500)],
            repair_options=[_option("skip", "营地灯", 200), _option("skip", "帽子", 150)],
        )
        assert verdict.state == "repairable"
        assert verdict.auto_applicable is False

    def test_放宽门限后可以自动改(self):
        verdict = judge(
            [], [_budget_issue(1800, 1600)],
            repair_options=[_option("downgrade", "睡袋", 250)],
            tau=0.2,
        )
        assert verdict.auto_applicable is True

    def test_省不回来就交还给用户取舍(self):
        # 预算按硬约束执行：修不动时问"减哪一块"，不问"放宽到多少"。
        verdict = judge([], [_budget_issue(3000, 1500)], repair_options=[_option("skip", "营地灯", 100)])
        assert verdict.state == "needs_input"
        assert verdict.ask["gap"] == 1500
        assert "减" in verdict.ask["question"]
        assert "放宽预算" not in verdict.ask["question"]

    def test_事实型问题不被信息缺口冲淡(self):
        # 温标不够是确定的，旁边还有"帐篷容量未知"也不能把它降级成"待确认"。
        constraint = Constraint(text="帐篷容量未知", status="unverified")
        issues = [
            Issue(constraint="帐篷：可容纳人数不够", severity="critical"),
            Issue(constraint="睡袋：温标未知", severity="minor"),
        ]
        verdict = judge([constraint], issues)
        assert verdict.state == "infeasible"

    def test_缺信息判为待确认并给出问题(self):
        constraint = Constraint(text="睡袋温标 ≤ -2℃", key="sleeping_bag_temp", status="unverified")
        issues = [
            _budget_issue(3000, 1500),
            Issue(constraint="睡袋：温标未知", severity="minor"),
        ]
        verdict = judge([constraint], issues)
        assert verdict.state == "needs_input"
        assert verdict.ask and "睡袋温标 ≤ -2℃" in verdict.ask["question"]

    def test_不可行时带上问题的出路(self):
        issue = Issue(
            constraint="装备：帐篷 无货",
            severity="critical",
            alternatives=[{"name": "速开帐篷", "price": 459}],
        )
        verdict = judge([], [issue])
        assert verdict.state == "infeasible"
        assert verdict.alternatives[0]["name"] == "速开帐篷"

    def test_结论可以序列化(self):
        verdict = judge([], [])
        assert isinstance(verdict, Verdict)
        assert verdict.model_dump()["state"] == "feasible"


class TestGaps:
    def test_未校验的约束算缺口(self):
        constraint = Constraint(text="睡袋温标 ≤ -2℃", key="sleeping_bag_temp", status="unverified")
        found = gaps([constraint], [])
        assert found[0]["subject"] == "睡袋温标 ≤ -2℃"
        assert found[0]["risk"] is True

    def test_口径类约束不算缺口(self):
        # 预算口径"不好自动判定"是设计如此，不是用户没说。
        constraint = Constraint(text="预算口径：含装备", key="budget_scope", status="unverified")
        assert gaps([constraint], []) == []

    def test_严重问题不算信息缺口(self):
        # 温标不够是事实，不是缺信息；把它归到"问一句就好"会误导用户。
        assert gaps([], [_budget_issue(3000, 1500)]) == []

    def test_去重(self):
        constraint = Constraint(text="温标不明", status="unverified")
        issue = Issue(constraint="睡袋：温标未知", severity="minor")
        found = gaps([constraint], [issue])
        assert len(found) == 2


def test_已说清的人数不会被当成缺口去问():
    # "人数：2 人"是已知信息，只是因为不便自动校验才标 unverified；
    # 拿它去问用户会显得系统没听懂。
    constraint = Constraint(text="人数：2 人", key="people", status="unverified")
    assert gaps([constraint], []) == []


class TestNeedsInputAsk:
    def test_问题里只列真有风险的项(self):
        people = Constraint(text="人数：2 人", key="people", status="unverified")
        bag = Constraint(text="睡袋温标 ≤ -2℃", key="sleeping_bag_temp", status="unverified")
        verdict = judge([people, bag], [_budget_issue(1800, 1500)])
        assert verdict.state == "needs_input"
        assert "人数" not in verdict.ask["question"]
        assert "睡袋温标" in verdict.ask["question"]


def test_display_only_constraints_are_not_asked_again():
    """出发地/城市/点名购买/预算分配是展示项，不该出现在 verify 追问里。"""
    from app.trip_gear.models import Constraint
    from app.trip_gear.verdict import gaps

    constraints = [
        Constraint(text="出发地：上海", key="origin", status="unverified"),
        Constraint(text="串联城市：汕头 → 广州 → 深圳", key="cities", status="unverified"),
        Constraint(text="点名购买：女生想买一套衣服", key="purchase", status="unverified"),
        Constraint(text="预算分配：行程 4500 / 装备 500", key="budget_split", status="unverified"),
        Constraint(text="目的地：广东", key="destination", status="unverified"),
    ]

    subjects = [item["subject"] for item in gaps(constraints, [])]

    assert "目的地：广东" in subjects
    assert not any("出发地" in s or "串联城市" in s or "点名购买" in s or "预算分配" in s for s in subjects)


class TestCode优先于文案:
    """预算类判定靠 code，不靠"预算"两个字开头。

    以前 ``budget_deficit`` 用 ``startswith("预算")``：流水线写"预算：3000 元"
    时恰好成立，但"总预算 3000 元"就不成立，超预算会被当成事实型冲突，
    直接把 needs_input 判成 infeasible。
    """

    def test_总预算文案也认得出是预算问题(self):
        verdict = judge(
            [],
            [Issue(constraint="总预算 2000 元", actual=2600, limit=2000, code="budget")],
            repair_options=[_option("skip", "营地灯", 10)],
        )
        assert verdict.state == "needs_input"
        assert verdict.ask["gap"] == 600

    def test_没有code时仍按前缀兜底(self):
        verdict = judge([], [_budget_issue(1800, 1500)], repair_options=[_option("skip", "x", 10)])
        assert verdict.state == "needs_input"

    def test_verify给预算问题打码(self):
        from app.trip_gear.models import DayPlan, Itinerary, ItineraryItem, TripFacts
        from app.trip_gear.verify import verify

        facts = TripFacts(destination="杭州", days=1, budget=100)
        constraints = [Constraint(text="预算：100 元", key="budget", source="用户原话")]
        itinerary = Itinerary(
            destination="杭州",
            days=[DayPlan(day=1, items=[ItineraryItem(time="10:00", activity="西湖", duration_min=60, cost=500)])],
        )
        issues = verify(facts, constraints, itinerary, [], {})
        assert [issue.code for issue in issues if issue.severity == "critical"] == ["budget"]


class Test追问顺序:
    def test_先问用户明说的硬条件(self):
        constraints = [
            Constraint(text="尽量住得舒服", source="模型推断", hard=True, status="unverified", key="lodging_x"),
            Constraint(text="必须带儿童安全座椅", source="用户原话", hard=True, status="unverified", key="kids_x"),
        ]
        subjects = [item["subject"] for item in gaps(constraints, [])]
        assert subjects[0] == "必须带儿童安全座椅"


class Test预算追问要说清差在哪:
    def test_写出实际与上限(self):
        verdict = judge(
            [],
            [Issue(constraint="预算：2000 元", actual=2477, limit=2000, code="budget")],
            repair_options=[],
        )
        assert verdict.state == "needs_input"
        assert "实际 2477 元" in verdict.ask["question"]
        assert "超 477 元" in verdict.ask["question"]
        assert verdict.ask["actual"] == 2477
        assert verdict.ask["limit"] == 2000

    def test_没有数字就不编具体差额(self):
        # 数值缺失时 budget_deficit 记 0，不会进"超了 X 元"这条路；
        # 这条只是钉住"别把字符串当数字算"。
        verdict = judge(
            [],
            [Issue(constraint="预算：2000 元", actual="超了", limit=2000, code="budget")],
            repair_options=[],
        )
        assert verdict.ask is None or "实际" not in str(verdict.ask.get("question"))
