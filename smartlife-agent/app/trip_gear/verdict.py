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
from app.trip_gear import tradeoff
from app.trip_gear.tradeoff import (
    DEFAULT_TAU,
    Delta,
    budget_delta,
    constraint_weight,
    make_term,
    parse_tau,
    source_confidence,
    total as sum_terms,
)

# 预算类 issue 的识别前缀，与 verify 里构造文案时保持一致。
# 前缀只是兜底：issue 带上 code 之后优先按 code 认，文案措辞改了也不会
# 把"超预算"错判成"事实型问题"进而把 needs_input 判成 infeasible。
# 动作档位的中文说明：为什么这个动作贵/便宜。
ACTION_KIND_LABEL = {
    "downgrade": "同品类降档（功能不丢）",
    "skip": "省略可选件（丢功能）",
    "change_lodging": "换住宿（改用户条件）",
    "soften_activity": "砍收费项目（改行程）",
}

BUDGET_PREFIXES = ("预算", "装备预算")
BUDGET_CODES = {"budget", "gear_budget"}

# 这些 code 是"说明"不是"违反"：省了哪几件、库里没匹配、可到店自提、
# 时效未知。把它们算进代价会把面板刷成一堵墙，也抬高了违反代价。
NON_COST_CODES = {"gear_skipped", "gear_unmatched", "self_pickup", "lead_unknown", "spec_unknown"}

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


def is_budget_issue(issue: Issue) -> bool:
    """这条 issue 是不是预算类。优先看 code，其次才看文案前缀。"""
    if str(issue.code or "") in BUDGET_CODES:
        return True
    return str(issue.constraint or "").startswith(BUDGET_PREFIXES)


def budget_deficit(issues: Iterable[Issue]) -> float:
    """预算缺口：actual - limit。取最大的一条，不累加（同一笔钱只缺一次）。"""
    worst = 0.0
    for issue in issues:
        if not is_budget_issue(issue):
            continue
        actual, limit = _num(issue.actual), _num(issue.limit)
        if actual is None or limit is None:
            continue
        worst = max(worst, actual - limit)
    return round(worst, 2)


def action_cost(option: Dict[str, Any]) -> float:
    """动作档位从 ``tradeoff`` 现取，不在这里复制一份表。

    好处是敏感性分析只需改一处常量就能验证"换档位会不会改变选择"，
    也不会出现"文档写 0.10、代码里是 0.15"这种两处口径。
    """
    kind = str(option.get("kind") or "")
    return tradeoff.ACTION_COST.get(kind, tradeoff.DEFAULT_ACTION_COST)


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


def _constraint_for(issue: Issue, by_key: Dict[str, Constraint]) -> Constraint:
    """找到 issue 对应的约束；没有（结构性问题）就按"硬约束·用户原话"记。"""
    key = str(issue.code or "")
    found = by_key.get(key)
    if found is not None:
        return found
    # 结构性 issue（时间重叠、商品未匹配）没有对应约束，按"规则推导的软项"
    # 记：既不是用户原话，也不该按硬约束满权重计。
    return Constraint(text=str(issue.constraint or ""), key=key, source="规则推导", hard=False)


def violation_terms(
    constraints: Iterable[Constraint],
    issues: Iterable[Issue],
) -> List[Any]:
    """把"违反了几条、每条多严重"折成带来源的代价项。

    这一层回答"权重从哪来"：``w = 硬度 × 来源置信度``，逐项可展示。
    它只用于**披露与排序**（先说不更硬的那条），不参与可行性判定——
    可行性只看"能不能消掉 critical"，这点不能动。
    """
    by_key = {item.key: item for item in constraints if item.key}
    terms = []
    for issue in issues:
        if str(issue.code or "") in NON_COST_CODES:
            continue
        constraint = _constraint_for(issue, by_key)
        confidence = source_confidence(constraint.source)
        weight = constraint_weight(constraint.hard, constraint.source)
        weight_detail = (
            f"{'硬' if constraint.hard else '软'}约束 × {constraint.source}"
            f"（置信度 {confidence:.1f}）= 权重 {weight:.2f}"
        )
        if is_budget_issue(issue):
            actual, limit = _num(issue.actual), _num(issue.limit)
            if actual is None or limit is None:
                delta = Delta(1.0, 1.0)
                delta_detail = "预算数值缺失，幅度按满幅 1.00"
            else:
                delta = budget_delta(actual, limit)
                over = max(0.0, actual - limit)
                delta_detail = f"超支 {over:.0f} 元 ÷ 预算 {limit:.0f} = {delta.raw:.2f}"
                if delta.raw > 1.0:
                    delta_detail += "，封顶 1.00"
        else:
            # 事实型冲突（温标不够、无货、天数不符）没有"超了多少"的连续幅度，
            # 按满幅计；它本来也不是靠代价来权衡的东西。
            delta = Delta(1.0, 1.0)
            delta_detail = "事实型冲突，幅度按满幅 1.00"
        terms.append(
            make_term(
                str(issue.constraint or ""),
                hard=constraint.hard,
                source=constraint.source,
                delta=delta,
                detail=f"{weight_detail}；{delta_detail}",
            )
        )
    return terms


def _ordered_blocking(constraints: Iterable[Constraint], critical: Iterable[Issue]) -> List[str]:
    """按权重从高到低给披露排序：用户说的硬约束排在最前。"""
    by_key = {item.key: item for item in constraints if item.key}
    weighted: List[tuple] = []
    seen = set()
    for issue in critical:
        label = str(issue.constraint or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        constraint = _constraint_for(issue, by_key)
        weighted.append((constraint_weight(constraint.hard, constraint.source), label))
    weighted.sort(key=lambda pair: -pair[0])
    return [label for _, label in weighted]


def gaps(constraints: Iterable[Constraint], issues: Iterable[Issue]) -> List[Dict[str, Any]]:
    """列出"还没核实"的项，这些是提问能解决的，不是修改能解决的。

    每项带 ``risk``：这个答案有没有可能推翻结论。没有风险的缺口照样披露，
    但不该打断用户——问了也改变不了什么，只是浪费一轮对话。
    """
    found: List[tuple] = []
    constraints = list(constraints)
    by_key = {item.key: item for item in constraints if item.key}
    for constraint in constraints:
        if constraint.key in NON_BLOCKING_KEYS:
            continue
        if constraint.status == "unverified":
            # 约束标了"要校验却没校验"，本身就可能藏着一条违反。
            found.append((
                constraint_weight(constraint.hard, constraint.source),
                {"kind": "constraint", "subject": constraint.text, "risk": True},
            ))
    for issue in issues:
        text = str(issue.constraint or "")
        if issue.severity == "critical":
            continue
        if any(marker in text for marker in UNKNOWN_MARKERS):
            constraint = _constraint_for(issue, by_key)
            found.append((
                constraint_weight(constraint.hard, constraint.source),
                {"kind": "fact", "subject": text, "risk": bool(issue.blocking_risk)},
            ))
    # 权重从高到低：先问"用户明说的硬条件"，再问规则推导、最后问模型推断。
    # 追问只有一轮、最多问三条，排序本身就是取舍——不能按字段出现顺序碰运气。
    found.sort(key=lambda pair: -pair[0])
    # 去重但保序：同一项可能既来自约束又来自 issue。
    seen = set()
    unique = []
    for _, item in found:
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

    # 代价明细覆盖所有违反（软约束违反也计代价，只是权重更低）；
    # 披露顺序只看 critical——"先说哪条"与"这条有多贵"是两件事。
    blocking = _ordered_blocking(constraints, critical)
    weighted_terms = violation_terms(constraints, issues)
    terms = [term.as_dict() for term in weighted_terms]
    violation_cost = round(sum_terms(weighted_terms), 6)

    if not critical:
        return Verdict(
            state="feasible",
            tau=tau_value,
            best_cost=0.0,
            auto_applicable=True,
            violation_terms=terms,
            violation_cost=violation_cost,
        )

    deficit = budget_deficit(critical)
    # 预算缺口之外的严重问题都是"事实型"：温标不够、无货、天数对不上。
    # 这类问题省钱修不掉，问用户也改变不了事实，所以直接判不可行——
    # 不能被无关的信息缺口冲淡成"待确认"。
    hard_facts = [issue for issue in critical if not is_budget_issue(issue)]
    plan = plan_repair(repair_options or [], deficit) if deficit > 0 else None

    if hard_facts:
        options: List[Dict[str, Any]] = []
        for issue in hard_facts:
            options.extend(issue.alternatives or [])
        return Verdict(
            state="infeasible",
            blocking=blocking,
            violation_terms=terms,
            violation_cost=violation_cost,
            tau=tau_value,
            best_cost=plan[0] if plan else 0.0,
            alternatives=options,
        )

    if plan is not None:
        cost, chosen = plan
        action_terms = [
            {
                "label": f"{option.get('kind')}:{option.get('name')}",
                "cost": action_cost(option),
                "saving": float(option.get("saving") or 0),
                "detail": (
                    f"{ACTION_KIND_LABEL.get(str(option.get('kind') or ''), '动作')}"
                    f"档位 {action_cost(option):.2f}；省 {float(option.get('saving') or 0):.0f} 元"
                ),
            }
            for option in chosen
        ]
        return Verdict(
            state="repairable",
            blocking=blocking,
            terms=action_terms,
            violation_terms=terms,
            violation_cost=violation_cost,
            best_cost=cost,
            tau=tau_value,
            auto_applicable=cost <= tau_value,
            alternatives=chosen,
        )

    # 只剩预算问题且省不回来：问用户"减哪一块"，不提议放宽预算——
    # 预算按硬约束执行，放宽是用户自己的决定，不该由系统先开口。
    risky = [item for item in gaps(constraints, issues) if item.get("risk")]
    if not risky and deficit <= 0:
        return Verdict(
            state="infeasible",
            blocking=blocking,
            violation_terms=terms,
            violation_cost=violation_cost,
            tau=tau_value,
        )
    ask = _ask_payload(risky)
    if deficit > 0:
        # 只说"还差 477 元"用户没法判断差在哪：他可能盯着行程那一栏（1500），
        # 而超的是"行程 + 装备"的合计。把实际、上限、超多少一起写出来。
        worst = max(
            (issue for issue in critical if is_budget_issue(issue)),
            key=lambda issue: (budget_deficit([issue])),
            default=None,
        )
        actual, limit = (None, None) if worst is None else (_num(worst.actual), _num(worst.limit))
        if actual is not None and limit is not None:
            shortfall = (
                f"预算 {limit:.0f} 元，实际 {actual:.0f} 元（含行程与装备），"
                f"超 {deficit:.0f} 元：要在预算内排下，得减一天、换更便宜的住宿，"
                "或者砍掉一项收费安排。你想减哪一块？"
            )
        else:
            shortfall = f"预算还差 {deficit:.0f} 元：减一天、换更便宜住宿，还是砍掉一项收费安排？"
        ask = {
            "question": shortfall + ("；" + ask["question"] if ask["question"] else ""),
            "missing": list(ask.get("missing") or []),
            "gap": deficit,
            "actual": actual,
            "limit": limit,
        }
    return Verdict(
        state="needs_input",
        blocking=blocking,
        violation_terms=terms,
        violation_cost=violation_cost,
        tau=tau_value,
        best_cost=0.0,
        ask=ask,
    )
