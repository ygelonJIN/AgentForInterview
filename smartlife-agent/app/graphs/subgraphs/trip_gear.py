"""装备协同子图：约束派生 → 逐日行程 → 装备推导 → 已有判定 → 缺口匹配 → 校验 → 修复。

纯 LangGraph 版本：``on_event`` 回调换成节点内 ``sink``，依赖从
``runtime.context.services.trip_gear_service`` 取，测试可用 ``context.extra`` 覆盖。
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.graphs._util import resolve
from app.graphs.context import TurnContext
from app.streaming import EventType
from app.trip_gear.constraints import (
    apply_assisted,
    derive_constraints,
    drop_owned_duplicates,
    enrich_weather,
    merge_inferred,
)
from app.trip_gear.gear import derive_gear, merge_suggestions
from app.trip_gear.itinerary import fallback_itinerary, total_cost
from app.trip_gear.models import Constraint, GearItem, Issue, Itinerary, TripFacts
from app.trip_gear.owned import (
    apply_owned,
    extract_from_memories,
    extract_owned,
    find_ambiguous_owned,
    merge_owned,
)
from app.trip_gear.render import render
from app.trip_gear import select_by_model
from app.trip_gear.select import rank as rank_products
from app.trip_gear.repair import RepairAction, apply_action, repair_candidates
from app.trip_gear.shopping import GearShopper
from app.trip_gear.preferences import extract_preferences
from app.trip_gear.spend import upgrade_within_budget
from app.trip_gear.spec_match import parse_specs
from app.trip_gear.verdict import judge
from app.trip_gear.verify import verify

CONSTRAINT_INFER_TIMEOUT = 6.0
# 槽位补全只在地名/天数抽不到时触发，属于反问前的最后一次兜底，给 4.5s 上限。
SLOT_ASSIST_TIMEOUT = 4.5
# 模型挑款是锦上添花：超时就用打分结果，不能拖住整轮。
MODEL_SELECT_TIMEOUT = 9.0

STEP_LABELS = {
    "derive": (1, "约束派生"),
    "plan": (2, "逐日行程"),
    "gear": (3, "装备推导"),
    "owned": (4, "已有装备"),
    "shop": (5, "缺口匹配"),
    "verify": (6, "预算与时间校验"),
    "repair": (7, "按预算调整"),
    "finalize": (7, "整理结果"),
}


class TripGearState(TypedDict, total=False):
    # 输入
    user_message: str
    user_id: str
    thread_id: str
    chat_history: List[Any]
    history_text: str
    max_repairs: int

    # 中间结果
    facts: Dict[str, Any]
    constraints: List[Dict[str, Any]]
    itinerary: Dict[str, Any]
    gear: List[Dict[str, Any]]
    suggestions: List[Dict[str, Any]]
    owned: List[Dict[str, Any]]
    owned_ambiguous: List[str]
    products: Dict[str, Dict[str, Any]]
    issues: List[Dict[str, Any]]
    revision: int
    # 修复候选：verify 节点算好，repair 每轮消费一条
    repair_options: List[Dict[str, Any]]
    # 校验结论：走哪条路（feasible / repairable / needs_input / infeasible）
    verdict: Dict[str, Any]
    tau: float
    # 校验后追问的轮数：问得出价值才问，且最多两轮
    clarify_rounds: int
    # 从长期记忆里抽出的选品偏好（品牌、住宿倾向），只做软提示
    preferences: Dict[str, Any]
    # 预算利用：把该花的钱花掉，记下升级了哪几处
    spend: Dict[str, Any]

    # 输出
    response: str
    status: str


def _service(runtime: Runtime[TurnContext]):
    return getattr(runtime.context.services, "trip_gear_service", None)


def _dep(runtime: Runtime[TurnContext], name: str, fallback: Optional[Callable[..., Any]] = None):
    """依赖优先级：context.extra 覆盖 > 服务方法 > 缺省。"""
    override = runtime.context.extra.get(name)
    if override is not None:
        return override
    service = _service(runtime)
    if service is None:
        return None
    return getattr(service, fallback or name, None)


async def _notify(runtime: Runtime[TurnContext], node: str, message: str) -> None:
    sink = runtime.context.sink_for(runtime)
    step, name = STEP_LABELS.get(node, (1, "装备规划"))
    try:
        await sink.emit(EventType.STEP, {
            "step": step,
            "total": 7,
            "name": name,
            "description": message,
            "status": "completed",
        }, step=node)
    except Exception:
        pass


async def derive(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    facts, constraints = derive_constraints(state["user_message"])
    history_text = str(state.get("history_text") or "")
    if (not facts.destination or not facts.days) and history_text:
        prior, _ = derive_constraints(history_text)
        inherited: List[str] = []
        if not facts.destination and prior.destination:
            facts.destination = prior.destination
            inherited.append(f"目的地：{prior.destination}")
        if not facts.days and prior.days:
            facts.days = prior.days
            inherited.append(f"天数：{prior.days} 天")
        if facts.people is None and prior.people:
            facts.people = prior.people
            inherited.append(f"人数：{prior.people} 人")
        if facts.budget is None and prior.budget is not None:
            facts.budget = prior.budget
            facts.budget_scope = prior.budget_scope
            inherited.append(f"预算：{prior.budget:g} 元")
            inherited.append(
                "预算口径：含装备" if prior.budget_scope == "all_in" else "预算口径：不含装备"
            )
        if facts.gear_budget is None and prior.gear_budget is not None:
            facts.gear_budget = prior.gear_budget
            inherited.append(f"装备预算：{prior.gear_budget:g} 元")
        if not facts.lodging and prior.lodging:
            facts.lodging = prior.lodging
            inherited.append("住宿：露营" if prior.lodging == "camp" else "住宿：酒店/民宿")
        if not facts.transport and prior.transport:
            facts.transport = prior.transport
            inherited.append(f"交通：{prior.transport}")
        if not facts.with_kids and prior.with_kids:
            facts.with_kids = True
            inherited.append("同行：有儿童")
        if not facts.start_date and prior.start_date:
            facts.start_date = prior.start_date
        for text in reversed(inherited):
            constraints.insert(0, Constraint(text=text, source="上文"))

    # 规则没抽到目的地/天数时，先给可选的小模型兜底一次再决定要不要反问
    missing_fields = [name for name in ("destination", "days") if not getattr(facts, name)]
    if missing_fields:
        assist = _dep(runtime, "slot_assist", "_assist_slots")
        if assist:
            try:
                assisted = await asyncio.wait_for(
                    resolve(assist(state["user_message"], missing_fields)),
                    timeout=SLOT_ASSIST_TIMEOUT,
                )
                if assisted:
                    facts, constraints = apply_assisted(facts, constraints, assisted)
            except Exception:
                assisted = None

    missing = []
    if not facts.destination:
        missing.append("目的地")
    if not facts.days:
        missing.append("天数")
    if missing:
        await _notify(runtime, "derive", "缺少核心信息，先澄清")
        return {
            "facts": facts.model_dump(),
            "constraints": [item.model_dump() for item in constraints],
            "status": "needs_info",
            "response": f"为了生成行程和装备清单，请先补充：{'、'.join(missing)}。",
        }

    weather_lookup = _dep(runtime, "weather_lookup", "_weather")
    weather = None
    if weather_lookup and facts.destination:
        try:
            weather = await resolve(weather_lookup(
                facts.destination,
                facts.start_date or date.today().isoformat(),
            ))
        except Exception:
            weather = None
    constraints = enrich_weather(facts, constraints, weather)

    # 规则优先、模型补充：只补规则漏掉的语义约束（带老人、不吃海鲜、要宽松行程等）。
    infer = _dep(runtime, "constraint_infer", "_infer_constraints")
    if infer and runtime.context.extra.get("model_constraints", True):
        try:
            # 约束推断是"锦上添花"，必须限时：模型慢或不可用不能拖住整轮。
            inferred = await asyncio.wait_for(
                resolve(infer(state["user_message"], facts, constraints)),
                timeout=CONSTRAINT_INFER_TIMEOUT,
            )
            if inferred:
                constraints = merge_inferred(constraints, inferred)
        except Exception:
            inferred = None

    await _notify(runtime, "derive", f"已派生 {len(constraints)} 条约束")
    return {
        "facts": facts.model_dump(),
        "constraints": [item.model_dump() for item in constraints],
        "status": "derived",
    }


async def plan(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    facts = TripFacts.model_validate(state["facts"])
    constraints = [Constraint.model_validate(item) for item in state["constraints"]]
    planner = _dep(runtime, "planner", "_plan")
    itinerary: Optional[Itinerary] = None
    suggestions: List[GearItem] = []
    if planner:
        try:
            result = await resolve(planner(facts, constraints))
            if isinstance(result, tuple) and len(result) == 2:
                itinerary, raw_suggestions = result
                suggestions = [
                    item if isinstance(item, GearItem) else GearItem.model_validate(item)
                    for item in (raw_suggestions or [])
                ]
            else:
                itinerary = result
        except Exception:
            itinerary = None
    if not isinstance(itinerary, Itinerary):
        itinerary = fallback_itinerary(facts)
    if not itinerary.destination:
        itinerary.destination = facts.destination
    await _notify(
        runtime, "plan",
        f"已生成 {len(itinerary.days)} 天行程和 {len(suggestions)} 条装备建议",
    )
    return {
        "itinerary": itinerary.model_dump(),
        "suggestions": [item.model_dump() for item in suggestions],
        "status": "planned",
    }


async def gear(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    facts = TripFacts.model_validate(state["facts"])
    itinerary = Itinerary.model_validate(state["itinerary"])
    derived = derive_gear(facts, itinerary, extra_text=state["user_message"])
    suggestions = [GearItem.model_validate(item) for item in state.get("suggestions", [])]
    derived = merge_suggestions(derived, suggestions)
    await _notify(runtime, "gear", f"已推导 {len(derived)} 项装备")
    return {"gear": [item.model_dump() for item in derived], "status": "gear_ready"}


async def owned(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    memories: List[Any] = []
    memory_loader = _dep(runtime, "memory_loader", "_memories")
    if memory_loader:
        try:
            memories = await resolve(
                memory_loader(state.get("user_id", ""), state["user_message"])
            ) or []
        except Exception:
            memories = []
    owned_items = merge_owned(
        extract_owned(state["user_message"]),
        extract_owned(str(state.get("history_text") or ""), source="上文"),
        extract_from_memories(memories),
    )
    items = [GearItem.model_validate(item) for item in state["gear"]]
    apply_owned(items, owned_items)
    preferences = extract_preferences(memories)

    # 已经有"已有"条目了，再把"记得带用户已有的 X"当约束摆出来就是重复
    constraints = [
        Constraint.model_validate(item) for item in state.get("constraints", [])
    ]
    constraints = drop_owned_duplicates(constraints, [item.name for item in owned_items])

    # "我有装备"这类模糊说法既不能算已有（会漏买），也不能当没说（会重复买），
    # 单独收集起来，在结果里给用户一个确认入口。
    ambiguous: List[str] = []
    for text in (state["user_message"], str(state.get("history_text") or "")):
        for phrase in find_ambiguous_owned(text):
            if phrase not in ambiguous:
                ambiguous.append(phrase)

    await _notify(
        runtime, "owned",
        f"核对到 {len(owned_items)} 项已有物品"
        + (f"，{len(ambiguous)} 处待确认" if ambiguous else ""),
    )
    return {
        "gear": [item.model_dump() for item in items],
        "constraints": [item.model_dump() for item in constraints],
        "owned": [item.model_dump() for item in owned_items],
        "owned_ambiguous": ambiguous,
        "preferences": preferences.as_dict(),
        "status": "owned_resolved",
    }


def _remaining_budget(facts: TripFacts, itinerary: Itinerary) -> Optional[float]:
    if facts.budget is None:
        return None
    if facts.budget_scope == "trip_only":
        return facts.gear_budget
    return max(0.0, float(facts.budget) - total_cost(itinerary))


async def shop(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    facts = TripFacts.model_validate(state["facts"])
    itinerary = Itinerary.model_validate(state["itinerary"])
    items = [GearItem.model_validate(item) for item in state["gear"]]
    products = dict(state.get("products") or {})
    product_search = _dep(runtime, "product_search", "_search")
    if product_search:
        preferred = (state.get("preferences") or {}).get("brands") or []
        shopper = GearShopper(product_search, preferred_brands=preferred, facts=facts)
        remaining = _remaining_budget(facts, itinerary)

        # 先让模型在已经打分裁剪过的候选里挑；没得挑、超时、选错都由打分兜底
        select_products = _dep(runtime, "product_select", "_select_products")
        if select_products:
            entries = select_by_model.build_entries(
                items,
                shopper.candidates_for,
                rank_products,
                facts=facts,
                remaining=remaining,
                preferred_brands=preferred,
            )
            if entries:
                try:
                    chosen = await asyncio.wait_for(
                        resolve(select_products(entries, facts, remaining)),
                        timeout=MODEL_SELECT_TIMEOUT,
                    )
                except Exception:
                    chosen = {}
                for name, product in (chosen or {}).items():
                    item = next((entry for entry in items if entry.name == name), None)
                    if item is None:
                        continue
                    products[name] = {
                        **product,
                        "match": {**(product.get("match") or {}), "by": "model"},
                    }
                    if remaining is not None:
                        remaining -= float(product.get("price") or 0) * item.qty

        shopper.fill(items, products, remaining_budget=remaining)

    # 预算不是上限而是计划：选完商品还有额度，就在同品类里换更好的款。
    spend: Dict[str, Any] = {}
    if product_search and facts.budget is not None:
        left = _remaining_budget(facts, itinerary)
        if left is not None:
            for item in items:
                if item.status != "need_buy":
                    continue
                product = products.get(item.name)
                if product:
                    left -= float(product.get("price") or 0) * item.qty
            # 没有专门的替代检索时，退回到商品检索本身——它能按 query 找同类。
            find_alternatives = (
                _dep(runtime, "alternatives", "_alternatives") or product_search
            )
            spend = upgrade_within_budget(
                facts, items, products, round(left, 2), find_alternatives
            ).as_dict()
    await _notify(
        runtime, "shop",
        f"已匹配 {len(products)} 件商品"
        + (f"，为用满预算升级 {len(spend.get('upgrades') or [])} 处" if spend else ""),
    )
    return {
        "gear": [item.model_dump() for item in items],
        "products": products,
        "spend": spend,
        "status": "shopped",
    }


async def verify_node(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    facts = TripFacts.model_validate(state["facts"])
    constraints = [Constraint.model_validate(item) for item in state["constraints"]]
    itinerary = Itinerary.model_validate(state["itinerary"])
    items = [GearItem.model_validate(item) for item in state["gear"]]
    # 替代检索只在"赶不上/无货"时才真的被调用，这里先拿到回调。
    find_alternatives = (
        _dep(runtime, "alternatives", "_alternatives")
        or _dep(runtime, "product_search", "_search")
    )
    issues = verify(
        facts, constraints, itinerary, items, state.get("products") or {},
        find_alternatives=find_alternatives,
    )
    repair_options: List[Dict[str, Any]] = []
    budget_blocked = any(
        str(issue.constraint).startswith(("预算", "装备预算")) and issue.severity == "critical"
        for issue in issues
    )
    if budget_blocked:
        candidates = repair_candidates(
            items,
            state.get("products") or {},
            find_alternatives=find_alternatives,
            facts=facts,
            itinerary=itinerary,
        )
        # 能自动执行的才进修复队列；要用户点头的（改住宿）只作为选项摆出来。
        repair_options = [
            action.as_dict() for action in candidates if not action.requires_confirm
        ]
        confirm_options = [
            action.as_dict() for action in candidates if action.requires_confirm
        ]
    else:
        confirm_options = []
    verdict = judge(
        constraints,
        issues,
        repair_options=repair_options,
        tau=state.get("tau"),
    )
    if confirm_options:
        # 需要确认的改法也递出去，让界面能问"要不要改成住民宿"。
        verdict.alternatives = list(verdict.alternatives) + confirm_options
    await _notify(
        runtime, "verify",
        f"发现 {len(issues)} 个问题，{len(repair_options)} 个可修复动作；"
        f"结论 {verdict.state}",
    )
    return {
        "issues": [issue.model_dump() for issue in issues],
        "constraints": [item.model_dump() for item in constraints],
        "repair_options": repair_options,
        "verdict": verdict.model_dump(),
        "status": "verified",
    }


def after_verify(state: TripGearState) -> str:
    """三条出路：能修就修；缺的是信息就问；其余直接出结果。

    判断依据从"有没有预算问题"换成"结论是哪一种"：温标不够、无货这类
    修不动的问题不会再被送进修复空转两轮。
    """
    verdict = state.get("verdict") or {}
    state_name = verdict.get("state")

    if (
        state_name == "repairable"
        and int(state.get("revision", 0)) < int(state.get("max_repairs", 2))
        and state.get("repair_options")
    ):
        return "repair"

    if state_name == "needs_input" and int(state.get("clarify_rounds", 0)) < MAX_VERIFY_CLARIFY_ROUNDS:
        return "verify_clarify"

    return "finalize"


async def verify_clarify(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """结论是"待确认"时发起一次有价值提问；补充后回校验复审。

    与 ``derive`` 后的 ``clarify`` 分工不同：那个补的是"能不能开始规划"的
    槽位（目的地、天数），这个补的是"方案到底行不行"的判断依据（温标、
    预算口径）。后者只有算过一遍方案才知道该问什么。
    """
    verdict = state.get("verdict") or {}
    ask = verdict.get("ask") or {}
    missing = list(ask.get("missing") or [])
    question = str(ask.get("question") or "还差一点信息才能判断，能补充一下吗？")
    rounds = int(state.get("clarify_rounds", 0))

    answer = interrupt(
        {
            "type": "trip_gear_verify_clarify",
            "missing": missing,
            "question": question,
            "verdict": verdict,
            "round": rounds,
        }
    )

    text = str(answer or "").strip()
    if not text:
        await _notify(runtime, "verify", "未收到补充信息，按当前结论输出")
        return {"status": "clarify_pending", "response": question}

    facts = TripFacts.model_validate(state["facts"])
    extra_facts, extra_constraints = derive_constraints(text)
    # 这一问是请用户改条件，所以补充可以覆盖原值。
    merged = _merge_facts(facts, extra_facts, overwrite=True)
    constraints = _merge_constraints(
        [Constraint.model_validate(item) for item in state.get("constraints", [])],
        extra_constraints,
    )
    items = [GearItem.model_validate(item) for item in state["gear"]]
    _apply_specs_from_text(items, text)

    await _notify(runtime, "verify", f"已收到补充，重新校验（第 {rounds + 1} 轮）")
    return {
        "facts": merged.model_dump(),
        "constraints": [item.model_dump() for item in constraints],
        "gear": [item.model_dump() for item in items],
        "clarify_rounds": rounds + 1,
        "status": "verify_clarified",
    }


def after_verify_clarify(state: TripGearState) -> str:
    """补充了就回校验复审；没补充就结束本轮，把问题直接回给用户。"""
    return "verify" if state.get("status") == "verify_clarified" else "end"


async def repair(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """执行一条修复动作：优先降档（保功能），其次省略可选件。"""
    items = [GearItem.model_validate(item) for item in state["gear"]]
    products = dict(state.get("products") or {})
    options = list(state.get("repair_options") or [])
    if not options:
        return {"status": "no_repair"}

    raw = options[0]
    action = RepairAction(
        kind=str(raw.get("kind") or "skip"),
        name=str(raw.get("name") or ""),
        saving=float(raw.get("saving") or 0),
        reason=str(raw.get("reason") or ""),
        replacement=raw.get("replacement"),
        meta=dict(raw.get("meta") or {}),
        requires_confirm=bool(raw.get("requires_confirm")),
    )
    facts = TripFacts.model_validate(state["facts"])
    itinerary = Itinerary.model_validate(state["itinerary"])
    apply_action(action, items, products, itinerary=itinerary, facts=facts)
    await _notify(runtime, "repair", action.reason or f"已调整 {action.name}")
    return {
        "gear": [item.model_dump() for item in items],
        "products": products,
        "itinerary": itinerary.model_dump(),
        "revision": int(state.get("revision", 0)) + 1,
        "repair_options": options[1:],
        "status": "repaired",
    }


async def finalize(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    text = render(
        TripFacts.model_validate(state["facts"]),
        [Constraint.model_validate(item) for item in state["constraints"]],
        Itinerary.model_validate(state["itinerary"]),
        [GearItem.model_validate(item) for item in state["gear"]],
        state.get("products") or {},
        [Issue.model_validate(item) for item in state.get("issues", [])],
        list(state.get("owned_ambiguous") or []),
        spend=state.get("spend") or {},
    )
    await _notify(runtime, "finalize", "完成")
    sink = runtime.context.sink_for(runtime)
    # 结构化结果单独发一条事件：正文照旧走 token 流，前端可用它渲染
    # 约束清单 / 逐日行程 / 装备清单 / 预算面板，而不必再解析 Markdown。
    try:
        await sink.emit("plan_result", {
            "facts": TripFacts.model_validate(state["facts"]).model_dump(),
            "constraints": [item.model_dump() for item in map(Constraint.model_validate, state["constraints"])],
            "itinerary": Itinerary.model_validate(state["itinerary"]).model_dump(),
            "gear": [item.model_dump() for item in map(GearItem.model_validate, state["gear"])],
            "products": state.get("products") or {},
            "issues": [item.model_dump() for item in map(Issue.model_validate, state.get("issues", []))],
            "owned_ambiguous": list(state.get("owned_ambiguous") or []),
            "verdict": state.get("verdict") or {},
            "revision": int(state.get("revision", 0)),
        }, step="finalize")
    except Exception:
        pass
    for start in range(0, len(text), 80):
        await sink.emit_token(text[start:start + 80], step="generate")
    return {"response": text, "status": "ok"}


MAX_VERIFY_CLARIFY_ROUNDS = 2


def _merge_facts(base: TripFacts, extra: TripFacts, overwrite: bool = False) -> TripFacts:
    """把补充信息并回事实。

    默认只填空槽位，不覆盖用户说过的话；``overwrite=True`` 用于"校验后追问"
    的回复——那一问本身就是请用户改条件（"放宽预算还是减内容"），
    这时新给的预算不是补充而是修正，必须采纳。
    """
    merged = base.model_copy(deep=True)
    if (not merged.destination or overwrite) and extra.destination:
        merged.destination = extra.destination
    if (not merged.days or overwrite) and extra.days:
        merged.days = extra.days
    if extra.people is not None and (merged.people is None or overwrite):
        merged.people = extra.people
    if (merged.budget is None and extra.budget is not None) or (overwrite and extra.budget is not None):
        merged.budget = extra.budget
        merged.budget_scope = extra.budget_scope
    if (not merged.lodging or overwrite) and extra.lodging:
        merged.lodging = extra.lodging
    if (not merged.transport or overwrite) and extra.transport:
        merged.transport = extra.transport
    if extra.with_kids and (not merged.with_kids or overwrite):
        merged.with_kids = True
    return merged


def _merge_constraints(
    current: List[Constraint],
    extra: List[Constraint],
) -> List[Constraint]:
    """新约束按 key 去重后追加，保持原有顺序。"""
    known_keys = {item.key for item in current if item.key}
    merged = list(current)
    for constraint in extra:
        if constraint.key and constraint.key in known_keys:
            continue
        merged.append(constraint)
        if constraint.key:
            known_keys.add(constraint.key)
    return merged


def _apply_specs_from_text(gear: List[GearItem], text: str) -> None:
    """把用户补充的规格落到对应装备上。

    问"睡袋温标多少"，用户答"零下十度"，这段文本不会变成约束，
    只有挂回装备本身，下一轮校验才看得见。
    """
    raw = str(text or "")
    if not raw:
        return
    for item in gear:
        if not item.name or item.name not in raw:
            continue
        for key, value in parse_specs(raw, item.name).items():
            item.specs.setdefault(key, value)


def after_derive(state: TripGearState) -> str:
    return "clarify" if state.get("status") == "needs_info" else "plan"


async def clarify(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """缺核心信息时中断并追问；恢复后把用户的补充并回事实，继续往下走。

    ``interrupt`` 在无 checkpointer 的场景下不会抛错，只是把本轮停下并返回
    ``__interrupt__``，所以这条路径对"不会有人恢复"的调用方也是安全的。
    """
    facts = TripFacts.model_validate(state["facts"])

    def _missing(target: TripFacts) -> List[str]:
        gaps: List[str] = []
        if not target.destination:
            gaps.append("目的地")
        if not target.days:
            gaps.append("天数")
        return gaps

    gaps = _missing(facts)
    if not gaps:
        return {"status": "clarified"}

    question = f"为了生成行程和装备清单，请先补充：{'、'.join(gaps)}。"
    answer = interrupt(
        {
            "type": "trip_gear_clarify",
            "missing": gaps,
            "question": question,
            "facts": facts.model_dump(),
        }
    )

    text = str(answer or "").strip()
    if not text:
        await _notify(runtime, "clarify", "未收到补充信息，本轮先停下")
        return {"status": "needs_info", "response": question}

    # 用补充文本重新派生，只填空缺槽位，不覆盖用户已说过的信息
    extra_facts, extra_constraints = derive_constraints(text)
    merged = _merge_facts(facts, extra_facts)

    constraints = [Constraint.model_validate(item) for item in state.get("constraints", [])]
    known_keys = {item.key for item in constraints if item.key}
    for constraint in extra_constraints:
        if constraint.key and constraint.key in known_keys:
            continue
        constraints.append(constraint)

    remaining = _missing(merged)
    if remaining:
        await _notify(runtime, "clarify", f"仍缺 {'、'.join(remaining)}")
        return {
            "facts": merged.model_dump(),
            "constraints": [item.model_dump() for item in constraints],
            "status": "needs_info",
            "response": f"还需要补充：{'、'.join(remaining)}。",
        }

    weather_lookup = _dep(runtime, "weather_lookup", "_weather")
    weather = None
    if weather_lookup and merged.destination:
        try:
            weather = await resolve(
                weather_lookup(
                    merged.destination,
                    merged.start_date or date.today().isoformat(),
                )
            )
        except Exception:
            weather = None
    constraints = enrich_weather(merged, constraints, weather)

    await _notify(runtime, "clarify", f"已补齐 {'、'.join(gaps)}，继续生成方案")
    return {
        "facts": merged.model_dump(),
        "constraints": [item.model_dump() for item in constraints],
        "status": "clarified",
        "response": "",
    }


def after_clarify(state: TripGearState) -> str:
    """补齐了就往 plan 走；没补齐就结束本轮，等用户下次说话。"""
    return "plan" if state.get("status") == "clarified" else "end"


def build_trip_gear_plan_graph(checkpointer: Optional[Any] = None):
    graph = StateGraph(TripGearState, context_schema=TurnContext)
    graph.add_node("derive", derive, timeout=30)
    graph.add_node("clarify", clarify, timeout=5)
    graph.add_node("plan", plan, timeout=120)
    graph.add_node("gear", gear, timeout=30)
    graph.add_node("owned", owned, timeout=30)
    graph.add_node("shop", shop, timeout=60)
    graph.add_node("verify", verify_node, timeout=30)
    graph.add_node("repair", repair, timeout=30)
    graph.add_node("finalize", finalize, timeout=30)
    graph.add_edge(START, "derive")
    graph.add_conditional_edges("derive", after_derive, {"clarify": "clarify", "plan": "plan"})
    graph.add_conditional_edges("clarify", after_clarify, {"plan": "plan", "end": END})
    graph.add_edge("plan", "gear")
    graph.add_edge("gear", "owned")
    graph.add_edge("owned", "shop")
    graph.add_edge("shop", "verify")
    graph.add_node("verify_clarify", verify_clarify, timeout=5)
    graph.add_conditional_edges(
        "verify",
        after_verify,
        {"repair": "repair", "verify_clarify": "verify_clarify", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "verify_clarify",
        after_verify_clarify,
        {"verify": "verify", "end": END},
    )
    graph.add_edge("repair", "verify")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


async def prepare(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """把父图的 user_message / chat_history 映射成装备子图需要的输入。"""
    ctx = runtime.context
    chat_history = state.get("chat_history")
    if chat_history is None:
        chat_history = ctx.services.get_chat_history(state.get("thread_id", ""))
    resolved_history = state.get("history_text")
    if not resolved_history:
        resolved_history = history_text(list(chat_history or []))
    return {
        "user_message": state.get("user_message", ""),
        "history_text": resolved_history,
        "max_repairs": int(state.get("max_repairs") or 2),
        "revision": int(state.get("revision") or 0),
        "status": "started",
    }


def build_trip_gear_graph(checkpointer: Optional[Any] = None):
    """顶层挂载点：先映射输入，再跑装备协同子图。"""
    plan_graph = build_trip_gear_plan_graph()

    graph = StateGraph(TripGearState, context_schema=TurnContext)
    graph.add_node("prepare", prepare, timeout=5)
    graph.add_node("trip_gear", plan_graph)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "trip_gear")
    graph.add_edge("trip_gear", END)
    return graph.compile(checkpointer=checkpointer)


def history_text(chat_history: Optional[List[Any]]) -> str:
    """只取用户说过的话：助手建议里出现“有帐篷”不能当成用户已有。"""
    lines = []
    for message in list(chat_history or [])[-10:]:
        if isinstance(message, dict):
            role = str(message.get("role", ""))
            content = message.get("content", "")
        else:
            role = str(getattr(message, "type", getattr(message, "role", "message")))
            content = getattr(message, "content", str(message))
        if role in {"user", "human"} and content:
            lines.append(str(content))
    return "\n".join(lines)
