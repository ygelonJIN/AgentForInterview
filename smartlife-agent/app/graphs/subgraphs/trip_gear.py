"""装备协同子图：约束派生 → 逐日行程 → 装备推导 → 已有判定 → 缺口匹配 → 校验 → 修复。

纯 LangGraph 版本：``on_event`` 回调换成节点内 ``sink``，依赖从
``runtime.context.services.trip_gear_service`` 取，测试可用 ``context.extra`` 覆盖。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.graphs._util import resolve
from app.graphs.context import TurnContext
from app.streaming import EventType
from app.trip_gear.constraints import derive_constraints, enrich_weather, merge_inferred
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
from app.trip_gear.repair import RepairAction, apply_action, repair_candidates
from app.trip_gear.shopping import GearShopper
from app.trip_gear.verify import verify

CONSTRAINT_INFER_TIMEOUT = 6.0

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
        if facts.people == 1 and prior.people > 1:
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
        "owned": [item.model_dump() for item in owned_items],
        "owned_ambiguous": ambiguous,
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
        GearShopper(product_search).fill(
            items, products, remaining_budget=_remaining_budget(facts, itinerary)
        )
    await _notify(runtime, "shop", f"已匹配 {len(products)} 件商品")
    return {
        "gear": [item.model_dump() for item in items],
        "products": products,
        "status": "shopped",
    }


async def verify_node(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    facts = TripFacts.model_validate(state["facts"])
    constraints = [Constraint.model_validate(item) for item in state["constraints"]]
    itinerary = Itinerary.model_validate(state["itinerary"])
    items = [GearItem.model_validate(item) for item in state["gear"]]
    issues = verify(facts, constraints, itinerary, items, state.get("products") or {})
    repair_options: List[Dict[str, Any]] = []
    budget_blocked = any(
        str(issue.constraint).startswith(("预算", "装备预算")) and issue.severity == "critical"
        for issue in issues
    )
    if budget_blocked:
        find_alternatives = _dep(runtime, "alternatives", "_alternatives")
        repair_options = [
            action.as_dict()
            for action in repair_candidates(
                items,
                state.get("products") or {},
                find_alternatives=find_alternatives,
            )
        ]
    await _notify(
        runtime, "verify",
        f"发现 {len(issues)} 个问题，{len(repair_options)} 个可修复动作",
    )
    return {
        "issues": [issue.model_dump() for issue in issues],
        "constraints": [item.model_dump() for item in constraints],
        "repair_options": repair_options,
        "status": "verified",
    }


def after_verify(state: TripGearState) -> str:
    budget_issue = any(
        str(issue.get("constraint", "")).startswith(("预算", "装备预算"))
        and issue.get("severity") == "critical"
        for issue in state.get("issues", [])
    )
    if (
        budget_issue
        and int(state.get("revision", 0)) < int(state.get("max_repairs", 2))
        and state.get("repair_options")
    ):
        return "repair"
    return "finalize"


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
    )
    apply_action(action, items, products)
    await _notify(runtime, "repair", action.reason or f"已调整 {action.name}")
    return {
        "gear": [item.model_dump() for item in items],
        "products": products,
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
            "revision": int(state.get("revision", 0)),
        }, step="finalize")
    except Exception:
        pass
    for start in range(0, len(text), 80):
        await sink.emit_token(text[start:start + 80], step="generate")
    return {"response": text, "status": "ok"}


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
    merged = facts.model_copy(deep=True)
    if not merged.destination and extra_facts.destination:
        merged.destination = extra_facts.destination
    if not merged.days and extra_facts.days:
        merged.days = extra_facts.days
    if merged.people <= 1 and extra_facts.people > 1:
        merged.people = extra_facts.people
    if merged.budget is None and extra_facts.budget is not None:
        merged.budget = extra_facts.budget
        merged.budget_scope = extra_facts.budget_scope
    if not merged.lodging and extra_facts.lodging:
        merged.lodging = extra_facts.lodging
    if not merged.transport and extra_facts.transport:
        merged.transport = extra_facts.transport
    if not merged.with_kids and extra_facts.with_kids:
        merged.with_kids = True

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
    graph.add_conditional_edges("verify", after_verify, {"repair": "repair", "finalize": "finalize"})
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
