"""校验结论：把"问题清单"变成"接下来走哪条路"。

``verify`` 回答"哪里不对"，本模块回答"那怎么办"。四态：

- ``feasible``    硬约束全满足，直接输出；
- ``repairable``  有动作能消除全部 critical，且省下的钱够补缺口；
- ``needs_input`` 改不动，但缺的是**信息**——问一句就能判断；
- ``infeasible``  改不动也问不出来，给替代方案与放弃清单。

两个刻意的设计：

1. **可行性只看能不能消除 critical，不看完美度**。用 τ 卡"可行不可行"会让
   结论随一个拍出来的数字漂移；τ 只用来判断"能不能不经用户确认就动手"
   （``auto_applicable``）。
2. **"修不动"和"不知道怎么修"要分开**。预算够不够是算术问题（有答案），
   睡袋温标未标注是信息问题（问一句就有答案）。混成一句"未满足约束"
   对用户没有任何帮助。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.trip_gear.models import Constraint, GearItem, Issue, TripFacts, Verdict
from app.trip_gear.tradeoff import DEFAULT_TAU, parse_tau

# 动作本身的代价档位：功能不丢的改动便宜，丢东西的改动贵。
ACTION_COST = {
    "downgrade": 0.10,
    "skip": 0.20,
    "change_lodging": 0.50,
    "soften_activity": 0.40,
}
DEFAULT_ACTION_COST = 0.30

# 预算类 issue 的识别前缀，与 verify 里构造文案时保持一致。
BUDGET_PREFIXES = ("预算", "装备预算")

# 表示"这一项压根没核实"的词，决定能不能归到"问一句就清楚"。
UNKNOWN_MARKERS = ("未知", "未定", "未标注", "不明", "无法判断")

# 这些约束"没自动校验"是设计如此（本来就不好判），不是信息缺失，
# 拿去问用户只会显得系统没听懂他刚说的话。对应 verify 里的 _NOT_AUTO/_INFORMATIONAL。
NON_BLOCKING_KEYS = {
    "people", "transport", "budget_scope", "weather",
    # 只做展示、没有确定性判定对象的条目：不要把它们塞进"还判断不了"的追问，
    # 用户刚说过"上海/汕头/买衣服/预算怎么分"，再问一遍等于没听懂。
    "origin", "cities", "purchase", "budget_split",
    "wants_activity", "fixed_event", "appointment", "start_date",
}


def _num(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def budget_deficit(issues: Iterable[Issue]) -> float:
    """预算缺口：actual - limit。取最大的一条，不累加（同一笔钱只缺一次）。"""
    worst = 0.0
    for issue in issues:
        text = str(issue.constraint or "")
        if not text.startswith(BUDGET_PREFIXES):
            continue
        actual, limit = _num(issue.actual), _num(issue.limit)
        if actual is None or limit is None:
            continue
        worst = max(worst, actual - limit)
    return round(worst, 2)


def action_cost(option: Dict[str, Any]) -> float:
    return ACTION_COST.get(str(option.get("kind") or ""), DEFAULT_ACTION_COST)


def plan_repair(
    options: Iterable[Dict[str, Any]],
    deficit: float,
) -> Optional[Tuple[float, List[Dict[str, Any]]]]:
    """挑一组动作把缺口补上；补不上返回 None。

    先按"代价低、省得多"排序，再累加直到够用——动作之间互不影响，
    所以不需要真做组合搜索。同价时优先省得多的，避免多改一件。
    """
    if deficit <= 0:
        return (0.0, [])

    ordered = sorted(
        (option for option in options if float(option.get("saving") or 0) > 0),
        key=lambda option: (action_cost(option), -float(option.get("saving") or 0)),
    )
    chosen: List[Dict[str, Any]] = []
    saved = 0.0
    cost = 0.0
    for option in ordered:
        chosen.append(option)
        saved += float(option.get("saving") or 0)
        cost += action_cost(option)
        if saved + 1e-9 >= deficit:
            return (round(cost, 4), chosen)
    return None


def gaps(constraints: Iterable[Constraint], issues: Iterable[Issue]) -> List[Dict[str, Any]]:
    """列出"还没核实"的项，这些是提问能解决的，不是修改能解决的。

    每项带 ``risk``：这个答案有没有可能推翻结论。没有风险的缺口照样披露，
    但不该打断用户——问了也改变不了什么，只是浪费一轮对话。
    """
    found: List[Dict[str, Any]] = []
    for constraint in constraints:
        if constraint.key in NON_BLOCKING_KEYS:
            continue
        if constraint.status == "unverified":
            # 约束标了"要校验却没校验"，本身就可能藏着一条违反。
            found.append({"kind": "constraint", "subject": constraint.text, "risk": True})
    for issue in issues:
        text = str(issue.constraint or "")
        if issue.severity == "critical":
            continue
        if any(marker in text for marker in UNKNOWN_MARKERS):
            found.append({
                "kind": "fact",
                "subject": text,
                "risk": bool(issue.blocking_risk),
            })
    # 去重但保序：同一项可能既来自约束又来自 issue。
    seen = set()
    unique = []
    for item in found:
        key = (item["kind"], item["subject"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _ask_payload(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    subjects = [item["subject"] for item in items][:3]
    if not subjects:
        return {"question": "", "missing": []}
    return {
        "question": "这几项目前还判断不了，补一句就能定：" + "、".join(subjects),
        "missing": subjects,
    }


def judge(
    constraints: List[Constraint],
    issues: List[Issue],
    repair_options: Optional[Iterable[Dict[str, Any]]] = None,
    tau: Optional[Any] = None,
) -> Verdict:
    """按 issue 与修复候选给出结论。"""
    tau_value = parse_tau(tau)
    critical = [issue for issue in issues if issue.severity == "critical"]

    if not critical:
        return Verdict(state="feasible", tau=tau_value, best_cost=0.0, auto_applicable=True)

    blocking = []
    for issue in critical:
        label = str(issue.constraint or "").strip()
        if label and label not in blocking:
            blocking.append(label)

    deficit = budget_deficit(critical)
    # 预算缺口之外的严重问题都是"事实型"：温标不够、无货、天数对不上。
    # 这类问题省钱修不掉，问用户也改变不了事实，所以直接判不可行——
    # 不能被无关的信息缺口冲淡成"待确认"。
    hard_facts = [
        issue for issue in critical
        if not str(issue.constraint or "").startswith(BUDGET_PREFIXES)
    ]
    plan = plan_repair(repair_options or [], deficit) if deficit > 0 else None

    if hard_facts:
        options: List[Dict[str, Any]] = []
        for issue in hard_facts:
            options.extend(issue.alternatives or [])
        return Verdict(
            state="infeasible",
            blocking=blocking,
            tau=tau_value,
            best_cost=plan[0] if plan else 0.0,
            alternatives=options,
        )

    if plan is not None:
        cost, chosen = plan
        terms = [
            {
                "label": f"{option.get('kind')}:{option.get('name')}",
                "cost": action_cost(option),
                "saving": float(option.get("saving") or 0),
            }
            for option in chosen
        ]
        return Verdict(
            state="repairable",
            blocking=blocking,
            terms=terms,
            best_cost=cost,
            tau=tau_value,
            auto_applicable=cost <= tau_value,
            alternatives=chosen,
        )

    # 只剩预算问题且省不回来：这是"取舍"不是"故障"，问用户放宽预算或减内容。
    risky = [item for item in gaps(constraints, issues) if item.get("risk")]
    if not risky and deficit <= 0:
        return Verdict(state="infeasible", blocking=blocking, tau=tau_value)
    ask = _ask_payload(risky)
    if deficit > 0:
        shortfall = f"预算还差 {deficit:.0f} 元，是放宽预算还是减掉一部分行程？"
        ask = {
            "question": shortfall + ("；" + ask["question"] if ask["question"] else ""),
            "missing": list(ask.get("missing") or []),
            "gap": deficit,
        }
    return Verdict(
        state="needs_input",
        blocking=blocking,
        tau=tau_value,
        best_cost=0.0,
        ask=ask,
    )
