"""顶层图的通用节点：准备、分类、收束、记忆提取。

所有节点签名统一为 ``(state, runtime)``；依赖从 ``runtime.context`` 取，
因此图只构建一次，任意并发轮次互不污染。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from app.services.task_models import build_composite_plan
from app.classifier import ClassificationResult, UnifiedClassifier
from app.graphs.context import TurnContext
from app.graphs.state import TurnState
from app.observability import log_exception
from app.streaming import EventType
from app.session import build_thread_id


async def prepare(state: TurnState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """建立 thread、记录用户消息、一次性派生共享约束。"""
    thread_id = build_thread_id(state["user_id"], state.get("scene", "general"), state["session_id"])
    await runtime.context.services.begin_turn(state["user_message"], thread_id)
    constraints = build_composite_plan(state["user_message"]).constraints
    # react 等子图按 messages 驱动，父图必须先播种，否则子图读到 KeyError。
    messages = list(state.get("messages") or [])
    if not messages:
        messages = [HumanMessage(content=state["user_message"])]
    return {
        "thread_id": thread_id,
        "constraints": constraints.model_dump(),
        "messages": messages,
        "status": "prepared",
    }


async def classify(state: TurnState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """统一分类 + 场景约束 + 关键词安全网（全部在服务层完成）。"""
    ctx = runtime.context
    raw = await ctx.services.classify(
        state["user_message"],
        state["thread_id"],
        ctx.sink_for(runtime),
    )
    classification = UnifiedClassifier.apply_scene_constraint(
        ClassificationResult.model_validate(raw.model_dump()),
        state.get("scene", "general"),
        state["user_message"],
    )
    # 场景策略纠正过意图时补发一条分类事件，避免界面上的意图与实际路由不一致。
    if (
        classification.intent != raw.intent
        or classification.effective_intents != raw.effective_intents
        or classification.sub_intent != raw.sub_intent
    ):
        await ctx.sink_for(runtime).emit(EventType.CLASSIFY, {
            "route": classification.route,
            "intent": classification.intent,
            "intents": classification.effective_intents,
            "sub_intent": classification.sub_intent,
            "retrieval": classification.retrieval,
            "confidence": classification.confidence,
            "reason": classification.reason,
            "method": "policy",
        }, step="classify")
    return {
        "classification": classification.model_dump(),
        "agent_used": getattr(classification, "agent_label", classification.intent),
        "status": "classified",
    }


_GEAR_SUB_INTENTS = {
    "gear_plan", "equipment_plan", "packing_list", "gear_checklist", "trip_gear",
}


def _is_gear_plan(classification: Dict[str, Any]) -> bool:
    """显式枚举 + 中文限定词，替代原来的 ``"gear" in sub_intent`` 包含判断。

    模型给的是自由文本（"装备清单规划"），只认"装备"二字会把
    "装备预算"之类的描述也劫持过来，所以要求同时出现清单/规划类词。
    """
    sub_intent = str(classification.get("sub_intent") or "").strip().lower()
    if sub_intent in _GEAR_SUB_INTENTS:
        return True
    if "装备" not in sub_intent:
        return False
    return any(word in sub_intent for word in ("清单", "规划", "准备", "带"))


def _is_trip_plan(classification: Dict[str, Any]) -> bool:
    """多天行程规划也交给 trip_gear。

    面板（约束/行程/装备/预算/校验）不是一套可以贴到任何回答上的样式，
    它需要结构化结果。travel 子图只产出正文，同样的请求在它那里永远没有
    面板；把规划类请求收敛到 trip_gear，用户看到的格式才一致。
    """
    return (
        str(classification.get("intent") or "") == "travel"
        and str(classification.get("sub_intent") or "") == "plan_trip"
        and str(classification.get("route") or "") == "plan_and_execute"
    )


def route(state: TurnState) -> str:
    """条件边：把分类结果翻译成下一个节点名。"""
    classification = state.get("classification") or {}
    intent = classification.get("intent", "general")
    route_name = classification.get("route", "react")
    intents = set(classification.get("intents") or [intent])
    if _is_gear_plan(classification):
        return "trip_gear"
    # 购物 + 旅行是复合请求，先走 composite；只有纯旅行的规划才收敛到 trip_gear。
    if {"shopping", "travel"}.issubset(intents):
        return "composite"
    if _is_trip_plan(classification) and intents <= {"travel"}:
        return "trip_gear"
    if intent == "shopping":
        return "shopping"
    if intent == "customer_service":
        return "customer_service"
    if intent == "travel":
        return "travel"
    if intent == "general":
        if route_name == "react":
            return "react"
        if route_name == "plan_and_execute":
            return "general_plan"
        return "general_chat"
    return "general_chat"


async def finalize(state: TurnState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """保存助手回复并发出统一 done 事件。"""
    ctx = runtime.context
    services = ctx.services
    classification = ClassificationResult.model_validate(state.get("classification") or {})
    await services.finish_turn(
        state.get("thread_id", ""),
        state.get("response", ""),
        classification,
        ctx.sink_for(runtime),
    )
    return {"status": "completed"}


async def extract_memory(state: TurnState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """候选记忆提取；失败只记录诊断，不影响已生成的回答。"""
    ctx = runtime.context
    sink = ctx.sink_for(runtime)
    try:
        result = await asyncio.to_thread(
            ctx.services.extract_candidate_memories_result,
            state["user_id"],
            state["session_id"],
            state["scene"],
            excluded_contents=state.get("excluded_memories", []),
            # 窗口由 UI 按游标切好，节点不再自己翻整段历史。
            messages=state.get("memory_window") or None,
        )
        payload = result.as_dict()
    except Exception as exc:
        log_exception("graph.memory_extract", exc)
        payload = {
            "candidates": [],
            "diagnostic": f"记忆提取失败：{type(exc).__name__}: {exc}",
            "status": "error",
            "has_error": True,
            "should_display": True,
        }
    payload["timing_ms"] = ctx.timing.metrics()
    await sink.emit("memory_extraction", payload, step="memory")
    return {"memory_extraction": payload, "status": "memory_extracted"}
