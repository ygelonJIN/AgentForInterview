"""装备协同领域服务：模型生成行程、工具取天气、记忆判已有、商品库补缺口。

控制流在 ``app.graphs.subgraphs.trip_gear`` 的图里；本模块只提供能力。
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field, ValidationError

from app.config import create_llm
from app.memory.long_term import LongTermMemory
from app.llm_json import ainvoke_json, extract_json_object as _extract_json, message_text as _text_of
from app.observability import log_exception
from app.retrieval.repositories import ProductRepository
from app.tools.travel_executor import TravelToolExecutor
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


# 不用 response_format / with_structured_output：部分 OpenAI 兼容服务不支持
# json_schema 模式（会直接 400），因此改成"提示词要 JSON + 本地解析 + 一次重试"。
_JSON_SCHEMA_HINT = """只输出一个 JSON 对象，不要任何解释文字，不要 markdown 代码块。
结构如下：
{
  "days": [
    {
      "day": 1,
      "items": [
        {"time": "09:00", "activity": "活动描述", "duration_min": 90, "cost": 0}
      ]
    }
  ],
  "gear": [
    {"name": "装备名", "qty": 1, "reason": "为什么带"}
  ]
}
要求：days 的数量必须等于给定天数；items 至少 3 条；cost 为整数元，免费或不确定写 0；
gear 只列这次出行需要准备的物品，不要身份证/手机/充电器这类通用物品。"""


class _InferredConstraint(BaseModel):
    """模型补充的约束条目。"""

    text: str = Field(description="一句可执行的约束描述，例如：要能洗热水澡")
    key: str = Field(
        default="",
        description=(
            "若属于已知类型请复用：destination/days/people/budget/budget_scope/"
            "gear_budget/lodging/transport/with_kids；否则写 other"
        ),
    )
    hard: bool = Field(default=False, description="是否必须满足；不确定写 false")


class _InferredConstraints(BaseModel):
    constraints: List[_InferredConstraint] = Field(default_factory=list)


class TripGearService:
    """装备协同领域服务：行程生成、天气、记忆、商品检索。"""
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

        messages = [
            SystemMessage(content=self.plan_prompt.messages[0].prompt.template),
            SystemMessage(content=_JSON_SCHEMA_HINT),
            HumanMessage(
                content=self.plan_prompt.messages[1].prompt.template.replace("{facts}", facts_text)
            ),
        ]

        # 两轮：首轮直接要 JSON；失败后把错误回灌，要求只输出 JSON 再试一次。
        last_error = ""
        for attempt in range(2):
            try:
                hint = (
                    messages
                    if attempt == 0
                    else messages + [
                        HumanMessage(
                            content=(
                                "上一次输出无法解析为要求的 JSON，错误：" + last_error +
                                "。请只输出一个 JSON 对象，不要解释、不要代码块标记。"
                            )
                        )
                    ]
                )
                response = await self.llm.ainvoke(hint)
                payload = _extract_json(_text_of(response))
                result = _PlanOutput.model_validate(payload)
                itinerary, suggestions = self._from_plan_output(facts, result)
                if itinerary is not None:
                    return itinerary, suggestions
                last_error = "days 为空"
            except (ValidationError, ValueError, TypeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:300]
            except Exception as exc:  # 网络、鉴权、模型不支持等
                log_exception("trip_gear.plan", exc)
                return fallback_itinerary(facts), []

        log_exception(
            "trip_gear.plan.parse",
            ValueError(last_error or "JSON 解析失败"),
            {"attempts": 2},
        )
        return fallback_itinerary(facts), []

    @staticmethod
    def _from_plan_output(
        facts: TripFacts,
        result: "_PlanOutput",
    ) -> tuple[Optional[Itinerary], List[GearItem]]:
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
                    if str(item.activity or "").strip()
                ],
                lodging=facts.lodging,
            )
            for day in result.days
        ]
        days = [day for day in days if day.items]
        if not days:
            return None, []
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

    async def _infer_constraints(
        self,
        message: str,
        facts: TripFacts,
        constraints: List[Constraint],
    ) -> List[Dict[str, Any]]:
        """规则优先、模型补充：只找规则没覆盖到的语义约束。"""
        known = "\n".join(f"- {item.text}" for item in constraints) or "（无）"
        messages = [
            SystemMessage(content=(
                "你是出行约束抽取器。规则已经抽出的约束如下，不要重复它们。\n"
                "只找规则漏掉的、对行程或装备有实际影响的信息，例如：\n"
                "- 同行人：带老人、带婴儿、有孕妇\n"
                "- 身体与偏好：不吃海鲜、怕晒、晕车、要能洗热水澡\n"
                "- 行程要求：要宽松不赶路、博物馆优先、不去人多的地方\n"
                "每条写成一句可执行的话，不要编造用户没说的内容。\n"
                "没有就返回空的 constraints 数组。"
            )),
            HumanMessage(content=f"已抽取约束：\n{known}\n\n用户原话：{message}"),
        ]
        try:
            result = await ainvoke_json(self.llm, messages, _InferredConstraints, None)
        except Exception as exc:
            log_exception("trip_gear.infer_constraints", exc)
            return []
        return [item.model_dump() for item in result.constraints]

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

    def _alternatives(
        self,
        subcategory: str = "",
        max_price: Optional[float] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """同子品类、价格上限内的候选商品，供修复时降档。"""
        if not subcategory:
            return []
        try:
            return self.product_repository.search(
                query="",
                category=subcategory,
                max_price=max_price,
                limit=limit,
            )
        except Exception:
            return []


