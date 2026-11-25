"""代价权重敏感性分析：换一套权重，结论会不会变。

为什么需要它
------------
代价函数的权重（硬度 1.0/0.6、来源置信度 1.0/0.9/0.8/0.5、动作档位
0.10–0.50、阈值 τ=0.15）都是工程设定，不是从数据里学出来的。论文里
必须回答"这些数字凭什么"，最诚实的答法不是再解释一遍，而是**扰动它们，
看结论翻不翻**：

- 排序翻转率低 → 结论不依赖某个具体数字，权重是稳定的；
- 翻转率高 → 老实说这是启发式，并指出哪一段最敏感。

扰动的三组，对应三个不同的问题：

1. **动作档位**：某一档 ±20%，选中的修复动作会不会换一套？
2. **来源置信度 / 软约束权重**：披露顺序与违反代价会不会换？
3. **τ**：只是"自动改还是问用户"的开关，**不该**改变可不可行——
   这一组同时也是对"τ 不参与可行性判定"的回归。

纯函数 + 进程内临时改常量，跑完即恢复；不调模型、不联网。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.trip_gear import tradeoff
from app.trip_gear.models import Constraint, Issue
from app.trip_gear.verdict import judge, plan_repair

# 动作档位扰动：一次只动一档。0.8/1.2 是"合理误差"，0.4 用来找
# "压到多少才会翻"的边界——只报 ±20% 不翻，读者没法判断余量有多大。
TIER_FACTORS = (0.8, 1.2, 0.4)

# 来源置信度里"模型推断"这一档的扰动值（其余档不动）。
MODEL_CONFIDENCE_VALUES = (0.3, 0.4, 0.6, 0.7, 0.9)

# 软约束权重的扰动值。
SOFT_WEIGHT_VALUES = (0.4, 0.5, 0.7, 0.8)

# 阈值扫描。
TAU_VALUES = (0.05, 0.10, 0.15, 0.20, 0.30)


def repair_cases() -> List[Dict[str, Any]]:
    """覆盖"单动作够 / 两动作才够 / 要确认 / 高代价"的缺口-选项组合。"""
    return [
        {"id": "downgrade-then-skip", "deficit": 300, "options": [
            {"kind": "skip", "name": "营地灯", "saving": 200},
            {"kind": "downgrade", "name": "睡袋", "saving": 250},
        ]},
        {"id": "single-skip", "deficit": 150, "options": [
            {"kind": "skip", "name": "帽子", "saving": 200},
        ]},
        {"id": "two-downgrades", "deficit": 200, "options": [
            {"kind": "downgrade", "name": "睡袋", "saving": 100},
            {"kind": "downgrade", "name": "帐篷", "saving": 150},
        ]},
        {"id": "skip-then-lodging", "deficit": 500, "options": [
            {"kind": "skip", "name": "乐园门票", "saving": 400},
            {"kind": "change_lodging", "name": "换民宿", "saving": 500},
        ]},
        {"id": "soften-activity", "deficit": 400, "options": [
            {"kind": "soften_activity", "name": "砍半天行程", "saving": 400},
        ]},
        {"id": "auto-downgrade", "deficit": 100, "options": [
            {"kind": "downgrade", "name": "酒店降档", "saving": 100},
        ]},
        {"id": "three-skips", "deficit": 300, "options": [
            {"kind": "skip", "name": "A", "saving": 100},
            {"kind": "skip", "name": "B", "saving": 100},
            {"kind": "skip", "name": "C", "saving": 100},
        ]},
        {"id": "lodging-only", "deficit": 600, "options": [
            {"kind": "change_lodging", "name": "换民宿", "saving": 700},
        ]},
    ]


def order_cases() -> List[Dict[str, Any]]:
    """混合硬软与来源，用来看披露顺序稳不稳。"""
    return [
        {
            "id": "user-hard-vs-model-hard",
            "constraints": [
                Constraint(text="预算：3000 元", key="budget", source="用户原话", hard=True),
                Constraint(text="睡袋温标 ≤ -2℃", key="sleeping_bag_temp", source="模型推断", hard=True),
            ],
            "issues": [
                Issue(constraint="预算：3000 元", actual=3200, limit=3000, code="budget"),
                Issue(constraint="睡袋温标 ≤ -2℃", actual=10, limit=-2, code="sleeping_bag_temp"),
            ],
        },
        {
            "id": "user-soft-vs-rule-hard",
            "constraints": [
                Constraint(text="尽量住得舒服", key="lodging", source="用户原话", hard=False),
                Constraint(text="露营必备装备", key="camping_gear", source="规则推导", hard=True),
            ],
            "issues": [
                Issue(constraint="尽量住得舒服", actual="青旅", limit="舒适", code="lodging", severity="minor"),
                Issue(constraint="露营必备装备", actual="缺防潮垫", limit="帐篷、睡袋、防潮垫", code="camping_gear"),
            ],
        },
        {
            "id": "rule-vs-model",
            "constraints": [
                Constraint(text="露营必备装备", key="camping_gear", source="规则推导", hard=True),
                Constraint(text="睡袋温标 ≤ -2℃", key="sleeping_bag_temp", source="模型推断", hard=True),
            ],
            "issues": [
                Issue(constraint="露营必备装备", actual="缺防潮垫", limit="三件", code="camping_gear"),
                Issue(constraint="睡袋温标 ≤ -2℃", actual=10, limit=-2, code="sleeping_bag_temp"),
            ],
        },
        {
            "id": "weather-vs-model",
            "constraints": [
                Constraint(text="降水概率 60%，备雨具", key="rain", source="天气", hard=True),
                Constraint(text="睡袋温标 ≤ -2℃", key="sleeping_bag_temp", source="模型推断", hard=True),
            ],
            "issues": [
                Issue(constraint="降水概率 60%，备雨具", actual="60%", limit="应备雨具", code="rain"),
                Issue(constraint="睡袋温标 ≤ -2℃", actual=10, limit=-2, code="sleeping_bag_temp"),
            ],
        },
    ]


@contextmanager
def _patched(**changes: Any):
    """临时改 tradeoff 的常量，退出时原样恢复。"""
    saved = {name: getattr(tradeoff, name) for name in changes}
    try:
        for name, value in changes.items():
            setattr(tradeoff, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(tradeoff, name, value)


def _plan_signature(deficit: float, options: Iterable[Dict[str, Any]]) -> Tuple:
    """只比"选了哪几个动作"。

    不把 cost 放进签名：档位整体缩放会让 cost 变，但选择没变，
    那不是翻转；把数值算进去会把"代价从 0.10 变 0.12"误报成结论变了。
    """
    plan = plan_repair(list(options), deficit)
    if plan is None:
        return ("none",)
    _, chosen = plan
    return tuple(option.get("name") for option in chosen)


def _baseline() -> Dict[str, Any]:
    return {
        "tau": tradeoff.DEFAULT_TAU,
        "hard_weight": tradeoff.HARD_WEIGHT,
        "soft_weight": tradeoff.SOFT_WEIGHT,
        "source_confidence": dict(tradeoff.SOURCE_CONFIDENCE_PREFIX),
        "action_cost": dict(tradeoff.ACTION_COST),
    }


def _order(verdict) -> Tuple:
    return tuple(verdict.blocking)


def _term_rank(verdict) -> Tuple:
    """违反代价从高到低的标签顺序：软约束权重变了，这里会变。"""
    terms = sorted(
        verdict.violation_terms,
        key=lambda term: float(term.get("cost") or 0),
        reverse=True,
    )
    return tuple(term.get("label") for term in terms)


def _judge_case(case: Dict[str, Any], deficit: float) -> Any:
    budget = Constraint(text="预算：3000 元", key="budget", source="用户原话", hard=True)
    issue = Issue(constraint="预算：3000 元", actual=3000 + deficit, limit=3000, code="budget")
    options = case.get("options") or []
    return judge([budget], [issue], repair_options=options, tau=tradeoff.DEFAULT_TAU)


def analyze() -> Dict[str, Any]:
    """跑三组扰动，返回逐项翻转率与一个总判定。"""
    cases = repair_cases()
    orders = order_cases()
    baseline_plans = {case["id"]: _plan_signature(case["deficit"], case["options"]) for case in cases}
    baseline_auto = {
        case["id"]: _judge_case(case, case["deficit"]).auto_applicable for case in cases
    }
    baseline_states = {
        case["id"]: _judge_case(case, case["deficit"]).state for case in cases
    }
    baseline_orders = {
        case["id"]: _order(judge(case["constraints"], case["issues"])) for case in orders
    }

    # 1. 动作档位：一档 ±20%，看选中的动作换不换
    tier_rows = []
    for kind in tradeoff.ACTION_COST:
        for factor in TIER_FACTORS:
            patched = dict(tradeoff.ACTION_COST)
            patched[kind] = round(tradeoff.ACTION_COST[kind] * factor, 4)
            with _patched(ACTION_COST=patched):
                flips = [
                    case["id"] for case in cases
                    if _plan_signature(case["deficit"], case["options"]) != baseline_plans[case["id"]]
                ]
                auto_flips = [
                    case["id"] for case in cases
                    if _judge_case(case, case["deficit"]).auto_applicable != baseline_auto[case["id"]]
                ]
                state_flips = [
                    case["id"] for case in cases
                    if _judge_case(case, case["deficit"]).state != baseline_states[case["id"]]
                ]
            tier_rows.append({
                "kind": kind,
                "factor": factor,
                "plan_flips": flips,
                "auto_flips": auto_flips,
                "state_flips": state_flips,
            })

    # 2. 来源置信度（模型推断档）与软约束权重：看披露顺序与违反代价
    source_rows = []
    for value in MODEL_CONFIDENCE_VALUES:
        patched = tuple(
            (prefix, value if prefix == "模型" else conf)
            for prefix, conf in tradeoff.SOURCE_CONFIDENCE_PREFIX
        )
        with _patched(SOURCE_CONFIDENCE_PREFIX=patched):
            changed = [
                case["id"] for case in orders
                if _order(judge(case["constraints"], case["issues"])) != baseline_orders[case["id"]]
            ]
        source_rows.append({"model_confidence": value, "order_flips": changed})

    soft_rows = []
    baseline_costs = {}
    baseline_ranks = {}
    for case in orders:
        verdict = judge(case["constraints"], case["issues"])
        baseline_costs[case["id"]] = verdict.violation_cost
        baseline_ranks[case["id"]] = _term_rank(verdict)
    for value in SOFT_WEIGHT_VALUES:
        with _patched(SOFT_WEIGHT=value):
            changed = []
            rank_changed = []
            for case in orders:
                verdict = judge(case["constraints"], case["issues"])
                if verdict.violation_cost != baseline_costs[case["id"]]:
                    changed.append(case["id"])
                if _term_rank(verdict) != baseline_ranks[case["id"]]:
                    rank_changed.append(case["id"])
        soft_rows.append({
            "soft_weight": value,
            "cost_changed": changed,
            "rank_flips": rank_changed,
        })

    # 3. τ：只该改"自动改/问用户"，不该改结论
    tau_rows = []
    for tau in TAU_VALUES:
        auto_changed = []
        state_changed = []
        for case in cases:
            budget = Constraint(text="预算：3000 元", key="budget", source="用户原话", hard=True)
            issue = Issue(constraint="预算：3000 元", actual=3000 + case["deficit"], limit=3000, code="budget")
            verdict = judge([budget], [issue], repair_options=case["options"], tau=tau)
            if verdict.auto_applicable != baseline_auto[case["id"]]:
                auto_changed.append(case["id"])
            if verdict.state != baseline_states[case["id"]]:
                state_changed.append(case["id"])
        tau_rows.append({"tau": tau, "auto_changed": auto_changed, "state_changed": state_changed})

    tier_flips = sum(len(row["plan_flips"]) for row in tier_rows)
    state_flips = sum(len(row["state_flips"]) for row in tier_rows) + sum(
        len(row["state_changed"]) for row in tau_rows
    )
    # "压到多少才会翻"：每档取最小的、已经出现翻转的倍率。
    first_flip = {}
    for kind in tradeoff.ACTION_COST:
        factors = sorted(
            row["factor"] for row in tier_rows
            if row["kind"] == kind and (row["plan_flips"] or row["auto_flips"])
        )
        first_flip[kind] = factors[0] if factors else None
    small = [row for row in tier_rows if row["factor"] in (0.8, 1.2)]
    stable_small = not any(row["plan_flips"] or row["auto_flips"] for row in small)
    return {
        "baseline": _baseline(),
        "cases": {
            "repair_cases": [case["id"] for case in cases],
            "order_cases": [case["id"] for case in orders],
        },
        "action_tier": {
            "perturbations": tier_rows,
            "plan_flip_total": tier_flips,
            "first_flip_factor": first_flip,
            "stable_under_pm20": stable_small,
        },
        "source_confidence": {"perturbations": source_rows},
        "soft_weight": {"perturbations": soft_rows},
        "tau": {"sweep": tau_rows},
        "state_flip_total": state_flips,
        "verdict": {
            "decisions_stable_under_pm20": stable_small,
            "disclosure_sensitive_to_model_confidence": any(
                row["order_flips"] for row in source_rows
            ),
            "disclosure_sensitive_to_soft_weight": any(
                row["rank_flips"] for row in soft_rows
            ),
            "tau_affects_feasibility": state_flips > 0,
        },
    }
