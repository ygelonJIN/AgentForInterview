"""trip_gear 的项目接线：模型生成行程，工具取天气，记忆判已有，商品库补缺口。"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.config import create_llm
from app.memory.long_term import LongTermMemory
from app.observability import log_exception
from app.retrieval.repositories import ProductRepository
from app.streaming import EventType
from app.tools.travel_executor import TravelToolExecutor
from app.trip_gear.graph import TripGearGraph
from app.trip_gear.itinerary import fallback_itinerary
from app.trip_gear.models import (
    Constraint,
    DayPlan,
    GearItem,
    Itinerary,
    ItineraryItem,
    TripFacts,
)

_STEP_LABELS = {
    "derive": (1, "约束派生"),
    "plan": (2, "逐日行程"),
    "gear": (3, "装备推导"),
    "owned": (4, "已有装备"),
    "shop": (5, "缺口匹配"),
    "verify": (6, "预算与时间校验"),
    "repair": (7, "按预算调整"),
    "finalize": (7, "整理结果"),
}


class _PlanItem(BaseModel):
    time: str = Field(default="", description="开始时间，HH:MM")
    activity: str = Field(description="做什么")
    duration_min: int = Field(default=90, ge=0, le=480, description="时长（分钟）")
    cost: float = Field(default=0, ge=0, description="估算花费（元），免费写 0")


class _PlanDay(BaseModel):
    day: int
    items: List[_PlanItem]


class _GearSuggestion(BaseModel):
    name: str = Field(description="装备名称")
    qty: int = Field(default=1, ge=1, le=20, description="数量")
    reason: str = Field(default="", description="为什么带")


class _PlanOutput(BaseModel):
    days: List[_PlanDay]
    gear: List[_GearSuggestion] = Field(default_factory=list)


class TripGearAgent:
    """一次请求跑完整条 trip_gear 链路。"""

    def __init__(
        self,
        model_name: Optional[str] = None,
        memory: Optional[LongTermMemory] = None,
        product_repository: Optional[ProductRepository] = None,
    ):
        self.model_name = model_name
        self.llm = create_llm(model_name)
        self._memory = memory
        self.product_repository = product_repository or ProductRepository()
        self.plan_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是行程规划师。根据给定事实排一份逐日行程。

要求：
1. 只安排真实可执行的活动，不编造景点、票价或实时信息；
2. 每天 4-6 个安排，包含用餐和休息，活动时间在 08:00-21:00 之间；
3. time 用 HH:MM；duration_min 用分钟；cost 用整数元估算，免费或不确定写 0；
4. 行程要贴合事实：住宿方式、天气、同行人、预算都要考虑；
5. 天数必须与给定天数一致；
6. 同时给出这次出行的装备建议（gear）：名称、数量、为什么带，覆盖衣物、鞋、洗护、药品、活动装备，不要列身份证/手机/充电器这类通用物品；
7. 装备要贴合场景：商务见客户要正装皮鞋，海边度假要裙装/泳衣/防晒，露营要帐篷睡袋。
"""),
            ("user", "{facts}"),
        ])

    @property
    def memory(self) -> Optional[LongTermMemory]:
        if self._memory is None:
            try:
                self._memory = LongTermMemory()
            except Exception:
                return None
        return self._memory

    async def _plan(
        self,
        facts: TripFacts,
        constraints: List[Constraint],
    ) -> tuple[Itinerary, List[GearItem]]:
        facts_text = "\n".join(
            [
                f"目的地：{facts.destination}",
                f"天数：{facts.days}",
                f"人数：{facts.people}",
                f"预算：{facts.budget if facts.budget is not None else '未指定'}"
                f"（{'含装备' if facts.budget_scope == 'all_in' else '不含装备'}）",
                f"住宿：{facts.lodging or '未指定'}",
                f"交通：{facts.transport or '未指定'}",
                f"同行儿童：{'有' if facts.with_kids else '无'}",
                f"天气：{facts.weather_summary or '未获取到可验证天气'}",
                "约束：\n" + "\n".join(f"- {item.text}" for item in constraints),
            ]
        )
        try:
            chain = self.plan_prompt | self.llm.with_structured_output(_PlanOutput)
            result = await chain.ainvoke({"facts": facts_text})
            days = [
                DayPlan(
                    day=day.day,
                    items=[
                        ItineraryItem(
                            time=item.time,
                            activity=item.activity,
                            duration_min=item.duration_min,
                            cost=item.cost,
                        )
                        for item in day.items
                    ],
                    lodging=facts.lodging,
                )
                for day in result.days
            ]
            if not days:
                return fallback_itinerary(facts), []
            suggestions = [
                GearItem(
                    name=item.name.strip(),
                    qty=max(1, int(item.qty or 1)),
                    reason=item.reason or "模型补充",
                )
                for item in result.gear
                if item.name and item.name.strip()
            ]
            return Itinerary(destination=facts.destination, days=days), suggestions
        except Exception as exc:
            log_exception("trip_gear.plan", exc)
            return fallback_itinerary(facts), []

    async def _weather(self, destination: str, date_str: str) -> Optional[Dict[str, Any]]:
        try:
            executor = TravelToolExecutor()
            evidence = await executor.get_weather_evidence(
                destination, date.fromisoformat(date_str)
            )
        except Exception:
            return None
        return evidence if evidence.get("accepted") else None

    async def _memories(self, user_id: str, query: str) -> List[Dict[str, Any]]:
        memory = self.memory
        if not memory:
            return []
        try:
            return memory.get_cross_scene_memories(user_id, "travel", query=query) or []
        except Exception:
            return []

    def _search(self, query: str = "", limit: int = 5) -> List[Dict[str, Any]]:
        try:
            return self.product_repository.search(query=query, limit=limit)
        except Exception:
            return []

    async def run_streaming(
        self,
        message: str,
        user_id: str = "user_001",
        queue=None,
        chat_history: Optional[List[Any]] = None,
    ) -> str:
        async def on_event(node: str, text: str) -> None:
            if not queue:
                return
            step, name = _STEP_LABELS.get(node, (1, "装备规划"))
            await queue.emit(
                EventType.STEP,
                {
                    "step": step,
                    "total": 7,
                    "name": name,
                    "description": text,
                    "status": "completed",
                },
                step=node,
            )

        graph = TripGearGraph(
            planner=self._plan,
            weather_lookup=self._weather,
            memory_loader=self._memories,
            product_search=self._search,
            on_event=on_event,
        )
        state = await graph.run(
            message,
            user_id,
            history_text=_history_text(chat_history),
        )
        text = str(state.get("response") or "")
        if queue and text:
            for chunk in _chunks(text, 80):
                await queue.emit_token(chunk, step="generate")
        return text

    def run(self, message: str, user_id: str = "user_001") -> str:
        import asyncio

        return asyncio.run(self.run_streaming(message, user_id))


def _chunks(text: str, size: int) -> List[str]:
    return [text[index:index + size] for index in range(0, len(text), size)]


def _history_text(messages: Optional[List[Any]]) -> str:
    """只取用户说过的话：助手建议里出现“有帐篷”不能当成用户已有。"""
    lines = []
    for message in list(messages or [])[-10:]:
        role = str(getattr(message, "type", getattr(message, "role", "message")))
        content = getattr(message, "content", str(message))
        if role in {"user", "human"} and content:
            lines.append(str(content))
    return "\n".join(lines)
