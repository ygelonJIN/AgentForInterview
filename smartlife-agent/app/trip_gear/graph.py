"""行程约束派生与装备协同规划的 LangGraph。

    约束派生 -> 逐日行程 -> 装备推导 -> 已有判定 -> 缺口匹配 -> 校验 -> 修复

图只做控制流；所有规则和数据结构都在同包的其他模块里，可单独测试。
"""
from __future__ import annotations

import inspect
from datetime import date
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.trip_gear.constraints import derive_constraints, enrich_weather
from app.trip_gear.gear import OPTIONAL, derive_gear, merge_suggestions
from app.trip_gear.itinerary import fallback_itinerary, total_cost
from app.trip_gear.models import Constraint, GearItem, Issue, Itinerary, TripFacts
from app.trip_gear.owned import apply_owned, extract_from_memories, extract_owned, merge_owned
from app.trip_gear.render import render
from app.trip_gear.shopping import GearShopper
from app.trip_gear.verify import verify


class TripGearState(TypedDict, total=False):
    message: str
    user_id: str
    history_text: str
    facts: Dict[str, Any]
    constraints: List[Dict[str, Any]]
    itinerary: Dict[str, Any]
    gear: List[Dict[str, Any]]
    suggestions: List[Dict[str, Any]]
    owned: List[Dict[str, Any]]
    products: Dict[str, Dict[str, Any]]
    issues: List[Dict[str, Any]]
    revision: int
    status: str
    response: str


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class TripGearGraph:
    """planner / weather / memory / search 全部可注入，方便离线测试。"""

    def __init__(
        self,
        planner: Optional[Callable[..., Any]] = None,
        weather_lookup: Optional[Callable[..., Any]] = None,
        memory_loader: Optional[Callable[..., Any]] = None,
        product_search: Optional[Callable[..., List[dict]]] = None,
        on_event: Optional[Callable[..., Any]] = None,
        max_repairs: int = 2,
    ):
        self.planner = planner
        self.weather_lookup = weather_lookup
        self.memory_loader = memory_loader
        self.product_search = product_search
        self.on_event = on_event
        self.max_repairs = max(0, int(max_repairs))
        self.graph = self._build_graph()

    async def _notify(self, node: str, message: str) -> None:
        if not self.on_event:
            return
        try:
            await _resolve(self.on_event(node, message))
        except Exception:
            pass

    async def _derive(self, state: TripGearState) -> Dict[str, Any]:
        facts, constraints = derive_constraints(state["message"])
        history_text = str(state.get("history_text") or "")
        if (not facts.destination or not facts.days) and history_text:
            prior, _prior_constraints = derive_constraints(history_text)
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
                    "预算口径：含装备"
                    if prior.budget_scope == "all_in"
                    else "预算口径：不含装备"
                )
            if facts.gear_budget is None and prior.gear_budget is not None:
                facts.gear_budget = prior.gear_budget
                inherited.append(f"装备预算：{prior.gear_budget:g} 元")
            if not facts.lodging and prior.lodging:
                facts.lodging = prior.lodging
                inherited.append(
                    "住宿：露营" if prior.lodging == "camp" else "住宿：酒店/民宿"
                )
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
            await self._notify("derive", "缺少核心信息，先澄清")
            return {
                "facts": facts.model_dump(),
                "constraints": [item.model_dump() for item in constraints],
                "status": "needs_info",
                "response": (
                    f"为了生成行程和装备清单，请先补充：{'、'.join(missing)}。"
                ),
            }

        weather = None
        if self.weather_lookup and facts.destination:
            try:
                weather = await _resolve(
                    self.weather_lookup(
                        facts.destination,
                        facts.start_date or date.today().isoformat(),
                    )
                )
            except Exception:
                weather = None
        constraints = enrich_weather(facts, constraints, weather)
        await self._notify("derive", f"已派生 {len(constraints)} 条约束")
        return {
            "facts": facts.model_dump(),
            "constraints": [item.model_dump() for item in constraints],
            "status": "derived",
        }

    async def _plan(self, state: TripGearState) -> Dict[str, Any]:
        facts = TripFacts.model_validate(state["facts"])
        constraints = [Constraint.model_validate(item) for item in state["constraints"]]
        itinerary: Optional[Itinerary] = None
        suggestions: List[GearItem] = []
        if self.planner:
            try:
                result = await _resolve(self.planner(facts, constraints))
                if isinstance(result, tuple) and len(result) == 2:
                    itinerary, raw_suggestions = result
                    suggestions = [
                        item if isinstance(item, GearItem)
                        else GearItem.model_validate(item)
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
        await self._notify(
            "plan",
            f"已生成 {len(itinerary.days)} 天行程和 {len(suggestions)} 条装备建议",
        )
        return {
            "itinerary": itinerary.model_dump(),
            "suggestions": [item.model_dump() for item in suggestions],
            "status": "planned",
        }

    async def _gear(self, state: TripGearState) -> Dict[str, Any]:
        facts = TripFacts.model_validate(state["facts"])
        itinerary = Itinerary.model_validate(state["itinerary"])
        gear = derive_gear(facts, itinerary, extra_text=state["message"])
        suggestions = [
            GearItem.model_validate(item)
            for item in state.get("suggestions", [])
        ]
        gear = merge_suggestions(gear, suggestions)
        await self._notify("gear", f"已推导 {len(gear)} 项装备")
        return {"gear": [item.model_dump() for item in gear], "status": "gear_ready"}

    async def _owned(self, state: TripGearState) -> Dict[str, Any]:
        memories: List[Any] = []
        if self.memory_loader:
            try:
                memories = await _resolve(
                    self.memory_loader(state.get("user_id", ""), state["message"])
                ) or []
            except Exception:
                memories = []
        owned = merge_owned(
            extract_owned(state["message"]),
            extract_owned(str(state.get("history_text") or ""), source="上文"),
            extract_from_memories(memories),
        )
        gear = [GearItem.model_validate(item) for item in state["gear"]]
        apply_owned(gear, owned)
        await self._notify("owned", f"核对到 {len(owned)} 项已有物品")
        return {
            "gear": [item.model_dump() for item in gear],
            "owned": [item.model_dump() for item in owned],
            "status": "owned_resolved",
        }

    @staticmethod
    def _remaining_budget(facts: TripFacts, itinerary: Itinerary) -> Optional[float]:
        if facts.budget is None:
            return None
        if facts.budget_scope == "trip_only":
            return facts.gear_budget
        return max(0.0, float(facts.budget) - total_cost(itinerary))

    async def _shop(self, state: TripGearState) -> Dict[str, Any]:
        facts = TripFacts.model_validate(state["facts"])
        itinerary = Itinerary.model_validate(state["itinerary"])
        gear = [GearItem.model_validate(item) for item in state["gear"]]
        products = dict(state.get("products") or {})
        if self.product_search:
            GearShopper(self.product_search).fill(
                gear,
                products,
                remaining_budget=self._remaining_budget(facts, itinerary),
            )
        await self._notify("shop", f"已匹配 {len(products)} 件商品")
        return {
            "gear": [item.model_dump() for item in gear],
            "products": products,
            "status": "shopped",
        }

    async def _verify(self, state: TripGearState) -> Dict[str, Any]:
        facts = TripFacts.model_validate(state["facts"])
        constraints = [Constraint.model_validate(item) for item in state["constraints"]]
        itinerary = Itinerary.model_validate(state["itinerary"])
        gear = [GearItem.model_validate(item) for item in state["gear"]]
        issues = verify(facts, constraints, itinerary, gear, state.get("products") or {})
        await self._notify("verify", f"发现 {len(issues)} 个问题")
        return {
            "issues": [issue.model_dump() for issue in issues],
            "constraints": [item.model_dump() for item in constraints],
            "status": "verified",
        }

    @staticmethod
    def _has_optional(state: TripGearState) -> bool:
        products = state.get("products") or {}
        return any(
            item.get("status") == "need_buy"
            and item.get("name") in OPTIONAL
            and item.get("name") in products
            for item in state.get("gear", [])
        )

    def _after_verify(self, state: TripGearState) -> str:
        budget_issue = any(
            str(issue.get("constraint", "")).startswith(("预算", "装备预算"))
            and issue.get("severity") == "critical"
            for issue in state.get("issues", [])
        )
        if (
            budget_issue
            and int(state.get("revision", 0)) < self.max_repairs
            and self._has_optional(state)
        ):
            return "repair"
        return "finalize"

    async def _repair(self, state: TripGearState) -> Dict[str, Any]:
        gear = [GearItem.model_validate(item) for item in state["gear"]]
        products = dict(state.get("products") or {})
        candidates = [
            item for item in gear
            if item.status == "need_buy"
            and item.name in OPTIONAL
            and item.name in products
        ]
        if not candidates:
            return {"status": "no_repair"}
        candidates.sort(
            key=lambda item: float(products[item.name].get("price") or 0) * item.qty,
            reverse=True,
        )
        target = candidates[0]
        target.status = "skipped"
        products.pop(target.name, None)
        await self._notify("repair", f"超出预算，先省略 {target.name}")
        return {
            "gear": [item.model_dump() for item in gear],
            "products": products,
            "revision": int(state.get("revision", 0)) + 1,
            "status": "repaired",
        }

    async def _finalize(self, state: TripGearState) -> Dict[str, Any]:
        text = render(
            TripFacts.model_validate(state["facts"]),
            [Constraint.model_validate(item) for item in state["constraints"]],
            Itinerary.model_validate(state["itinerary"]),
            [GearItem.model_validate(item) for item in state["gear"]],
            state.get("products") or {},
            [Issue.model_validate(item) for item in state.get("issues", [])],
        )
        await self._notify("finalize", "完成")
        return {"response": text, "status": "ok"}

    @staticmethod
    def _after_derive(state: TripGearState) -> str:
        return "clarify" if state.get("status") == "needs_info" else "plan"

    def _build_graph(self):
        graph = StateGraph(TripGearState)
        graph.add_node("derive", self._derive)
        graph.add_node("plan", self._plan)
        graph.add_node("gear", self._gear)
        graph.add_node("owned", self._owned)
        graph.add_node("shop", self._shop)
        graph.add_node("verify", self._verify)
        graph.add_node("repair", self._repair)
        graph.add_node("finalize", self._finalize)

        graph.add_edge(START, "derive")
        graph.add_conditional_edges(
            "derive",
            self._after_derive,
            {"clarify": END, "plan": "plan"},
        )
        graph.add_edge("plan", "gear")
        graph.add_edge("gear", "owned")
        graph.add_edge("owned", "shop")
        graph.add_edge("shop", "verify")
        graph.add_conditional_edges(
            "verify",
            self._after_verify,
            {"repair": "repair", "finalize": "finalize"},
        )
        graph.add_edge("repair", "verify")
        graph.add_edge("finalize", END)
        return graph.compile()

    async def run(
        self,
        message: str,
        user_id: str = "",
        history_text: str = "",
    ) -> Dict[str, Any]:
        return await self.graph.ainvoke(
            {
                "message": message,
                "user_id": user_id,
                "history_text": history_text,
                "constraints": [],
                "gear": [],
                "products": {},
                "issues": [],
                "revision": 0,
                "status": "started",
            }
        )
