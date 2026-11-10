"""装备协同子图：约束派生 → 逐日行程 → 装备推导 → 已有判定 → 缺口匹配 → 校验 → 修复。

纯 LangGraph 版本：``on_event`` 回调换成节点内 ``sink``，依赖从
``runtime.context.services.trip_gear_service`` 取，测试可用 ``context.extra`` 覆盖。
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.graphs._util import resolve
from app.graphs.context import TurnContext
from app.nlu import lexicon
from app.streaming import EventType
from app.trip_gear.constraints import (
    _vague_city_request,
    apply_assisted,
    derive_constraints,
    drop_owned_duplicates,
    ensure_destination,
    ensure_people,
    ensure_start_date,
    enrich_air_quality,
    enrich_exchange_rate,
    enrich_holidays,
    enrich_weather,
    merge_inferred,
    merge_province_cities,
)
from app.trip_gear.gear import derive_gear, merge_suggestions
from app.trip_gear.itinerary import apply_fixed_events, fallback_itinerary, total_cost
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
# 外部事实（天气/空气质量/汇率/假日）并发取，每条单独限时。
# provider 自己是 10s×3 重试，串起来最坏两分钟；并发 + 单条 8s 才有上限。
# 8s 是按冷启动实测定的：一次地理编码 + 一次预报约 4~5s，4s 会把多城市全掐断。
FACT_TIMEOUT_SECONDS = 8.0

STEP_LABELS = {
    "derive": (1, "约束派生"),
    "plan": (2, "逐日行程"),
    "gear": (3, "装备推导"),
    "owned": (4, "已有装备"),
    "shop": (5, "缺口匹配"),
    "verify": (6, "预算与时间校验"),
    "repair": (7, "按预算调整"),
    "finalize": (7, "整理结果"),
    # 追问补答不是第 2 条链路：它仍然属于"把约束补齐"，别掉进 _notify 的
    # 兜底值 (1, "装备规划")——那会让补答那一步显示成"1/7 · 装备规划"。
    "clarify": (1, "约束派生"),
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
    # 必须用户点头才能执行的修复（如露营改住民宿）：verify 摆出来，repair_confirm 问
    confirm_options: List[Dict[str, Any]]
    confirm_rounds: int
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


async def _load_weather(
    runtime: Runtime[TurnContext],
    destination: str,
    start_date: Optional[str],
    days: Optional[int],
    cities: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """逐日天气优先：行程有几天就查几天，只有单日接口时退回一天。

    拿第 1 天代表整趟是最省事的错法：第 2 天降温、第 3 天下雨，装备和安排
    都得跟着变，而约束清单里只看得到一个数字，用户没法发现被漏掉了。
    """
    place = str(destination or "").strip()
    if not place:
        return None
    start = str(start_date or "") or date.today().isoformat()
    forecast = _dep(runtime, "weather_forecast", "_weather_forecast")

    # 多城市：按"城市→天"的分法逐天取天气，避免拿一个城市的天气代表整趟
    city_list = [str(city) for city in (cities or []) if city]
    if len(city_list) >= 2 and forecast is not None:
        from app.trip_gear.itinerary import assign_cities

        span = int(days or 1)
        assignment = assign_cities(span, city_list)

        async def _one(city: str):
            """同步依赖走线程，异步依赖直接 await；都要能被单条超时掐断。"""
            try:
                if inspect.iscoroutinefunction(forecast):
                    return await asyncio.wait_for(
                        forecast(city, start, span), FACT_TIMEOUT_SECONDS
                    )
                return await asyncio.wait_for(
                    asyncio.to_thread(forecast, city, start, span),
                    FACT_TIMEOUT_SECONDS,
                )
            except Exception:
                return None

        payloads = await asyncio.gather(*[_one(city) for city in city_list])
        rows: List[Dict[str, Any]] = []
        for index in range(1, span + 1):
            city = assignment.get(index) or city_list[0]
            payload = payloads[city_list.index(city)] if payloads else None
            target = (date.fromisoformat(start) + timedelta(days=index - 1)).isoformat()
            row = next(
                (
                    item
                    for item in ((payload or {}).get("days") or [])
                    if str(item.get("date") or "") == target
                ),
                None,
            )
            if row:
                rows.append({**row, "city": city})
        return {"days": rows} if rows else None

    if forecast is not None:
        try:
            result = await resolve(forecast(place, start, int(days or 1)))
            if result:
                return result
        except Exception:
            pass
    lookup = _dep(runtime, "weather_lookup", "_weather")
    if lookup is None:
        return None
    try:
        return await resolve(lookup(place, start))
    except Exception:
        return None


def _trip_dates(start_date: Optional[str], days: Optional[int]) -> List[str]:
    """行程覆盖的日期列表；没给出发日期就按今天起算，和天气口径一致。"""
    try:
        start = date.fromisoformat(str(start_date or "")[:10]) if start_date else date.today()
    except ValueError:
        start = date.today()
    span = max(1, int(days or 1))
    return [(start + timedelta(days=offset)).isoformat() for offset in range(span)]


async def _load_air_quality(
    runtime: Runtime[TurnContext],
    destination: str,
    start_date: Optional[str],
    days: Optional[int],
) -> Optional[Dict[str, Any]]:
    """逐日空气质量；它是补充证据，取不到就不写（天气已经负责降级说明）。"""
    place = str(destination or "").strip()
    if not place:
        return None
    if int(days or 1) > 5:
        return None  # 空气质量接口只给 5 天，超出不查也不猜
    lookup = _dep(runtime, "air_quality", "_air_quality")
    if lookup is None:
        return None
    start = str(start_date or "") or date.today().isoformat()
    try:
        return await resolve(lookup(place, start, int(days or 1)))
    except Exception:
        return None


async def _load_exchange_rate(
    runtime: Runtime[TurnContext],
    destination: str,
) -> tuple[Optional[Dict[str, Any]], Optional[tuple]]:
    """境外目的地才查汇率；表外目的地不猜货币。"""
    from app.tools.travel_facts import currency_for

    currency = currency_for(destination)
    if not currency:
        return None, None
    lookup = _dep(runtime, "exchange_rate", "_exchange_rate")
    if lookup is None:
        return None, currency
    try:
        return await resolve(lookup("CNY", currency[0])), currency
    except Exception:
        return None, currency


async def _load_holidays(
    runtime: Runtime[TurnContext],
    destination: str,
    start_date: Optional[str],
    days: Optional[int],
) -> Optional[list]:
    """只查国内法定假日（CN），且必须出发日期明确。

    没说日期时按"今天"推出来的假日提示是误报：用户下周才走，今天是不是假期
    跟他这趟没关系。所以这里宁可不出这条，也不拿默认值凑。
    """
    if not start_date:
        return None
    from app.tools.travel_facts import currency_for

    if currency_for(destination):
        return None  # 境外不查：接口只有国家维度，货币表外也不猜
    lookup = _dep(runtime, "holidays", "_holidays")
    if lookup is None:
        return None
    dates = _trip_dates(start_date, days)
    years = sorted({str(item)[:4] for item in dates if item})
    hits: List[Dict[str, Any]] = []
    for year in years:
        try:
            payload = await resolve(lookup("CN", int(year)))
        except Exception:
            continue
        for item in (payload or {}).get("holidays") or []:
            if str(item.get("date") or "") in dates:
                hits.append(item)
    return hits


async def _guard_fact(coro):
    """单条事实限时；超时或异常都算"没拿到"，不影响其他事实。"""
    try:
        return await asyncio.wait_for(coro, timeout=FACT_TIMEOUT_SECONDS)
    except Exception:
        return None


async def _load_facts(
    runtime: Runtime[TurnContext],
    facts: TripFacts,
) -> Dict[str, Any]:
    """四个外部事实并发取，谁慢谁超时，彼此不拖累。

    串行是之前的写法：天气 + 空气质量 + 汇率 + 假日一次出行就吃掉七八秒，
    provider 重试时更久。这里并发 + 单条 4s，整轮才有上限。
    """
    jobs = {
        "weather": _load_weather(
            runtime, facts.destination, facts.start_date, facts.days, facts.cities
        ),
        "air_quality": _load_air_quality(
            runtime, facts.destination, facts.start_date, facts.days
        ),
        "exchange_rate": _load_exchange_rate(runtime, facts.destination),
        "holidays": _load_holidays(
            runtime, facts.destination, facts.start_date, facts.days
        ),
    }
    values = await asyncio.gather(*[_guard_fact(job) for job in jobs.values()])
    return dict(zip(jobs, values))


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
        if not facts.origin and prior.origin:
            facts.origin = prior.origin
            inherited.append(f"出发地：{prior.origin}")
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
        date_source = ""
        if not facts.start_date and prior.start_date:
            facts.start_date = prior.start_date
            date_source = "上文"
        for text in reversed(inherited):
            constraints.insert(0, Constraint(text=text, source="上文"))
        # 日期可能从上文补上：清单里那条"未指定（按今天计）"要换成真实日期。
        constraints = ensure_start_date(facts, constraints, source=date_source)

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

    # 省份不是城市：广东、浙江这类没有天气和路线可言，排出来只会是"广东某地"。
    if facts.destination in lexicon.PROVINCES and len(facts.cities) < 2:
        await _notify(runtime, "derive", "目的地是省份，需要具体城市")
        return {
            "facts": facts.model_dump(),
            "constraints": [item.model_dump() for item in constraints],
            "status": "needs_info",
            "response": (
                f"「{facts.destination}」是省份，不是城市：天气、路线和住宿都要按城市查。"
                "告诉我具体去哪些城市（比如「广州、深圳、珠海」），我按这几个城市排。"
            ),
        }

    missing = _blocking_gaps(facts)
    if missing:
        await _notify(runtime, "derive", "缺少核心信息，先澄清")
        return {
            "facts": facts.model_dump(),
            "constraints": [item.model_dump() for item in constraints],
            "status": "needs_info",
            "response": f"为了生成行程和装备清单，请先补充：{'、'.join(missing)}。",
        }

    facts_bag = await _load_facts(runtime, facts)
    constraints = enrich_weather(facts, constraints, facts_bag.get("weather"))
    constraints = enrich_air_quality(facts, constraints, facts_bag.get("air_quality"))
    rate, currency = facts_bag.get("exchange_rate") or (None, None)
    constraints = enrich_exchange_rate(facts, constraints, rate, currency)
    constraints = enrich_holidays(facts, constraints, facts_bag.get("holidays"))

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
    else:
        # 模型生成的行程也要把已订活动钉进去，否则"买好票的音乐会"会被排丢
        itinerary = apply_fixed_events(itinerary, facts)
    if not itinerary.destination:
        itinerary.destination = facts.destination
    # 多城市：模型没写 city 的按"顺序均分"补齐，渲染和天气都依赖这个字段
    if len(facts.cities) >= 2:
        from app.trip_gear.itinerary import assign_cities

        assignment = assign_cities(len(itinerary.days), facts.cities)
        for day in itinerary.days:
            if not day.city:
                day.city = assignment.get(day.day, "")
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
        "confirm_options": confirm_options,
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

    # 自动修复只能动装备；改住宿这种会推翻用户明说的条件，必须问一次。
    # 摆一个具体方案比泛泛问"放宽预算还是减内容"更有价值，所以排在追问之前。
    if state.get("confirm_options") and int(state.get("confirm_rounds", 0)) < MAX_REPAIR_CONFIRM_ROUNDS:
        return "repair_confirm"

    if state_name == "needs_input" and int(state.get("clarify_rounds", 0)) < MAX_VERIFY_CLARIFY_ROUNDS:
        return "verify_clarify"

    return "finalize"


def _accepts_confirm(text: str) -> bool:
    """识别"同意调整"的回复；否决词优先，免得"不要改"被当成"改"。"""
    value = str(text or "").strip().lower()
    if not value:
        return False
    if any(word in value for word in (
        "不用", "不要", "不改", "不换", "不调", "保持", "算了", "取消", "否", "别",
    )):
        return False
    return any(word in value for word in (
        "调整", "改", "可以", "行", "好", "确认", "同意", "换", "住", "是的", "对", "ok",
    ))


async def repair_confirm(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """把"必须用户点头"的修复问出来：同意才执行，不同意就保留原方案。

    这类动作（典型是露营改住民宿）会推翻用户明说的条件，``repair`` 节点
    的 ``apply_action`` 默认拒绝执行；只有这里拿到明确同意才 ``force=True``。
    """
    options = list(state.get("confirm_options") or [])
    rounds = int(state.get("confirm_rounds", 0))
    if not options:
        return {"status": "confirm_skipped", "confirm_options": []}

    raw = options[0]
    action = RepairAction(
        kind=str(raw.get("kind") or "skip"),
        name=str(raw.get("name") or ""),
        saving=float(raw.get("saving") or 0),
        reason=str(raw.get("reason") or ""),
        replacement=raw.get("replacement"),
        meta=dict(raw.get("meta") or {}),
        requires_confirm=True,
    )
    answer = interrupt(
        {
            "type": "trip_gear_repair_confirm",
            "question": (
                f"{action.reason}。"
                "回复「调整」我就按这个方案改，回复「不用」就保留原样。"
            ),
            "action": action.as_dict(),
            "round": rounds,
        }
    )

    if not _accepts_confirm(answer):
        await _notify(runtime, "repair", "保留原方案，未做调整")
        return {
            "status": "confirm_declined",
            "confirm_options": [],
            "confirm_rounds": rounds + 1,
        }

    items = [GearItem.model_validate(item) for item in state["gear"]]
    products = dict(state.get("products") or {})
    facts = TripFacts.model_validate(state["facts"])
    itinerary = Itinerary.model_validate(state["itinerary"])
    apply_action(action, items, products, itinerary=itinerary, facts=facts, force=True)
    await _notify(runtime, "repair", action.reason or f"已按你的确认调整 {action.name}")
    return {
        "gear": [item.model_dump() for item in items],
        "products": products,
        "itinerary": itinerary.model_dump(),
        "revision": int(state.get("revision", 0)) + 1,
        "confirm_options": [],
        "confirm_rounds": rounds + 1,
        "status": "confirmed",
    }


async def verify_clarify(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """结论是"待确认"时发起一次有价值提问；补充后回校验复审。

    与 ``derive`` 后的 ``clarify`` 分工不同：那个补的是"能不能开始规划"的
    槽位（目的地、天数），这个补的是"方案到底行不行"的判断依据（温标、
    预算口径）。后者只有算过一遍方案才知道该问什么。
    """
    from app.services.clarify_questions import build_questions

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
            "questions": build_questions(missing),
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
    # 这一问可能就是在改期，日期要跟着改，清单里的占位也要换掉。
    constraints = ensure_start_date(merged, constraints, source="追问补充")
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
# 需要用户点头的改法只问一轮：问第二遍就是纠缠，不是帮忙。
MAX_REPAIR_CONFIRM_ROUNDS = 1


def _merge_facts(base: TripFacts, extra: TripFacts, overwrite: bool = False) -> TripFacts:
    """把补充信息并回事实。

    默认只填空槽位，不覆盖用户说过的话；``overwrite=True`` 用于"校验后追问"
    的回复——那一问本身就是请用户改条件（"放宽预算还是减内容"），
    这时新给的预算不是补充而是修正，必须采纳。
    """
    merged = base.model_copy(deep=True)
    if (not merged.origin or overwrite) and extra.origin:
        merged.origin = extra.origin
    if extra.cities and (not merged.cities or overwrite):
        merged.cities = list(extra.cities)
    # 之前给的是省份，这次答了具体城市：以具体城市为准
    if extra.destination and merged.destination in lexicon.PROVINCES:
        merged.destination = extra.destination
    if (not merged.destination or overwrite) and extra.destination:
        merged.destination = extra.destination
    if (not merged.days or overwrite) and extra.days:
        merged.days = extra.days
    if extra.people is not None and (merged.people is None or overwrite):
        merged.people = extra.people
    # 日期一直没合并：用户在追问里答了"11月20号出发"也会被静默丢掉。
    if extra.start_date and (not merged.start_date or overwrite):
        merged.start_date = extra.start_date
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


def _blocking_gaps(facts: TripFacts) -> List[str]:
    """能不能开始规划：缺一样就先问，不许拿默认值顶上去当用户说的。

    人数和出发日期以前是"可见默认值"（按 1 人 / 按今天），结果是用户看着
    一份按 1 人算的预算和一份按今天算的天气，还得自己发现哪里被假设了。
    现在四项一起问，点选项就能补，补完照旧重写清单。
    """
    gaps: List[str] = []
    if not facts.destination:
        gaps.append("目的地")
    elif facts.destination in lexicon.PROVINCES and len(facts.cities) < 2:
        gaps.append("具体城市")
    if not facts.days:
        gaps.append("天数")
    if facts.people is None:
        gaps.append("人数")
    if not facts.start_date:
        gaps.append("出发日期")
    return gaps


def after_derive(state: TripGearState) -> str:
    return "clarify" if state.get("status") == "needs_info" else "plan"


async def clarify(state: TripGearState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """缺核心信息时中断并追问；恢复后把用户的补充并回事实，继续往下走。

    ``interrupt`` 在无 checkpointer 的场景下不会抛错，只是把本轮停下并返回
    ``__interrupt__``，所以这条路径对"不会有人恢复"的调用方也是安全的。
    """
    facts = TripFacts.model_validate(state["facts"])

    gaps = _blocking_gaps(facts)
    if not gaps:
        return {"status": "clarified"}

    if "具体城市" in gaps:
        question = (
            f"「{facts.destination}」是省份，不是城市：天气、路线和住宿都要按城市查。"
            "告诉我要去哪些城市（比如「广州、深圳、珠海」）。"
        )
    else:
        question = f"为了生成行程和装备清单，请先补充：{'、'.join(gaps)}。"
    from app.services.clarify_questions import build_questions

    answer = interrupt(
        {
            "type": "trip_gear_clarify",
            "missing": gaps,
            "question": question,
            "facts": facts.model_dump(),
            # 一次问多个：每题三个可点选项 + 一个自己填
            "questions": build_questions(gaps, facts),
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
    # "广东的很多城市"是授权我们挑，不是缺信息：点名城市保留，其余用常用城市补
    if (
        merged.destination in lexicon.PROVINCES
        and len(merged.cities) < 2
        and _vague_city_request(text)
    ):
        merged.cities = merge_province_cities(
            merged.destination, merged.cities, merged.days
        )
        if merged.cities and "cities" not in known_keys:
            constraints.append(Constraint(
                text=f"串联城市：{' → '.join(merged.cities)}",
                source="规则推导（省份常用城市）",
                key="cities",
            ))

    # 目的地可能从省份换成具体城市，日期可能从占位换成真实值：都要重写，不能并存。
    # 来源只在值真的变了时才写"追问补充"——用户这轮只回了人数，不该把目的地和
    # 日期一起记成追问得来的，面板上的"可回溯"会失真。
    def _source_of(key: str, changed: bool, default: str = "用户原话") -> str:
        if changed:
            return "追问补充"
        return next((item.source for item in constraints if item.key == key), default)

    constraints = ensure_destination(
        merged, constraints,
        source=_source_of("destination", merged.destination != facts.destination),
    )
    constraints = ensure_people(
        merged, constraints,
        source=_source_of("people", merged.people != facts.people),
    )
    constraints = ensure_start_date(
        merged, constraints,
        source=_source_of("start_date", merged.start_date != facts.start_date),
    )

    remaining = _blocking_gaps(merged)
    if remaining:
        await _notify(runtime, "clarify", f"仍缺 {'、'.join(remaining)}")
        return {
            "facts": merged.model_dump(),
            "constraints": [item.model_dump() for item in constraints],
            "status": "needs_info",
            "response": f"还需要补充：{'、'.join(remaining)}。",
        }

    facts_bag = await _load_facts(runtime, merged)
    constraints = enrich_weather(merged, constraints, facts_bag.get("weather"))
    constraints = enrich_air_quality(merged, constraints, facts_bag.get("air_quality"))
    rate, currency = facts_bag.get("exchange_rate") or (None, None)
    constraints = enrich_exchange_rate(merged, constraints, rate, currency)
    constraints = enrich_holidays(merged, constraints, facts_bag.get("holidays"))

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
    graph.add_node("repair_confirm", repair_confirm, timeout=5)
    graph.add_conditional_edges(
        "verify",
        after_verify,
        {
            "repair": "repair",
            "repair_confirm": "repair_confirm",
            "verify_clarify": "verify_clarify",
            "finalize": "finalize",
        },
    )
    graph.add_edge("repair_confirm", "verify")
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
