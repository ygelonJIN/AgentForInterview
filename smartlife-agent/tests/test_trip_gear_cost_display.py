"""代价明细的可解释性：面板与正文都要说清"多贵、省多少、权重从哪来"。

之前 ``verdict.terms`` 只在单测里被断言过，界面上一个字都没有——
"每项代价可展示"只写在注释里。这组测试把它钉在显示层。
"""
from __future__ import annotations

from app.trip_gear.models import Constraint, DayPlan, Issue, Itinerary, TripFacts
from app.trip_gear.render import render
from app.trip_gear.verdict import judge
from app.ui.theme import plan_panels_html


def _payload(verdict: dict) -> dict:
    return {
        "facts": {"budget": 3000, "destination": "杭州", "people": 1, "budget_scope": "all_in"},
        "constraints": [],
        "itinerary": {"destination": "杭州", "days": [
            {"day": 1, "items": [{"time": "10:00", "activity": "西湖", "cost": 1500}],
             "lodging_cost": 400},
        ]},
        "gear": [],
        "products": {},
        "issues": [
            {"constraint": "预算：3000 元", "actual": 3200, "limit": 3000,
             "hint": "含装备总花费超出预算", "severity": "critical"},
        ],
        "verdict": verdict,
    }


def test_面板展示违反代价与权重来源():
    verdict = judge(
        [Constraint(text="预算：3000 元", key="budget", source="用户原话")],
        [Issue(constraint="预算：3000 元", actual=3200, limit=3000, code="budget")],
        repair_options=[{"kind": "downgrade", "name": "酒店降档", "saving": 200}],
    ).model_dump()

    html = plan_panels_html(_payload(verdict))

    assert "违反代价" in html
    assert "代价 = 权重 × 幅度" in html
    assert "权重 1.00" in html
    assert "不决定可不可行" in html
    # 过程要看得见：权重怎么来的、幅度怎么算的
    assert "硬约束 × 用户原话" in html
    assert "超支 200 元 ÷ 预算 3000" in html


def test_面板展示修复动作代价与阈值():
    verdict = judge(
        [Constraint(text="预算：3000 元", key="budget", source="用户原话")],
        [Issue(constraint="预算：3000 元", actual=3200, limit=3000, code="budget")],
        repair_options=[{"kind": "downgrade", "name": "酒店降档", "saving": 200}],
    ).model_dump()

    html = plan_panels_html(_payload(verdict))

    assert "按代价从小到大" in html
    assert "阈值 τ" in html
    assert "省 ¥200" in html


def test_正文也有代价与取舍():
    text = render(
        facts=TripFacts(destination="杭州", days=1, budget=3000),
        constraints=[Constraint(text="预算：3000 元", key="budget")],
        itinerary=Itinerary(destination="杭州", days=[DayPlan(day=1, items=[])]),
        gear=[],
        products={},
        issues=[Issue(constraint="预算：3000 元", actual=3200, limit=3000, code="budget")],
        verdict=judge(
            [Constraint(text="预算：3000 元", key="budget", source="用户原话")],
            [Issue(constraint="预算：3000 元", actual=3200, limit=3000, code="budget")],
            repair_options=[{"kind": "downgrade", "name": "酒店降档", "saving": 200}],
        ).model_dump(),
    )

    assert "## 代价与取舍" in text
    assert "违反 预算：3000 元" in text
    assert "权重 1.00 × 幅度" in text
    assert "算式：" in text


def test_权重按来源分档且用户原话最硬():
    user_hard = judge(
        [Constraint(text="预算：3000 元", key="budget", source="用户原话")],
        [Issue(constraint="预算：3000 元", actual=3200, limit=3000, code="budget")],
        repair_options=[{"kind": "skip", "name": "x", "saving": 200}],
    )
    model_soft = judge(
        [Constraint(text="预算：3000 元", key="budget", source="模型推断", hard=False)],
        [Issue(constraint="预算：3000 元", actual=3200, limit=3000, code="budget")],
        repair_options=[{"kind": "skip", "name": "x", "saving": 200}],
    )

    assert user_hard.violation_terms[0]["weight"] == 1.0
    assert model_soft.violation_terms[0]["weight"] < 1.0
    assert user_hard.violation_cost > model_soft.violation_cost


def test_披露顺序按权重排序():
    verdict = judge(
        [
            Constraint(text="睡袋温标 ≤ -2℃", key="sleeping_bag_temp", source="模型推断", hard=True),
            Constraint(text="预算：3000 元", key="budget", source="用户原话", hard=True),
        ],
        [
            Issue(constraint="睡袋温标 ≤ -2℃", actual=10, limit=-2, code="sleeping_bag_temp"),
            Issue(constraint="预算：3000 元", actual=3200, limit=3000, code="budget"),
        ],
        repair_options=[{"kind": "skip", "name": "x", "saving": 200}],
    )

    assert verdict.blocking[0] == "预算：3000 元"
