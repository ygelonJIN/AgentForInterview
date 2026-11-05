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


def route(state: TurnState) -> str:
    """条件边：把分类结果翻译成下一个节点名。"""
    classification = state.get("classification") or {}
    intent = classification.get("intent", "general")
    route_name = classification.get("route", "react")
    intents = set(classification.get("intents") or [intent])
    sub_intent = str(classification.get("sub_intent") or "").strip()
    # 模型给的是自由文本（"装备清单规划"），不能只认 "gear_plan" 这一个字面值。
    if sub_intent == "gear_plan" or "gear" in sub_intent.lower() or "装备" in sub_intent:
        return "trip_gear"
    if {"shopping", "travel"}.issubset(intents):
        return "composite"
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
