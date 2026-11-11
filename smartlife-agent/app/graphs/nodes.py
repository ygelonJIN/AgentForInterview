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


def _sanitize_history(messages: List[Any]) -> List[Any]:
    """去掉会让 thinking 模型 400 的 assistant 工具消息，以及它们的 tool 结果。

    只丢"没有 reasoning_content 的 tool_calls 消息"——有 reasoning 的照常保留，
    新一轮拼出来的消息本来就不带 tool_calls。
    """
    dropped_calls: set = set()
    kept: List[Any] = []
    for message in messages:
        kind = getattr(message, "type", "")
        if kind in {"ai", "assistant"} and getattr(message, "tool_calls", None):
            reasoning = (getattr(message, "additional_kwargs", None) or {}).get(
                "reasoning_content"
            )
            if reasoning:
                kept.append(message)
                continue
            for call in message.tool_calls or []:
                if isinstance(call, dict) and call.get("id"):
                    dropped_calls.add(call["id"])
            continue
        if kind == "tool" and getattr(message, "tool_call_id", "") in dropped_calls:
            continue
        kept.append(message)
    return kept


async def prepare(state: TurnState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    """建立 thread、记录用户消息、一次性派生共享约束。"""
    thread_id = build_thread_id(state["user_id"], state.get("scene", "general"), state["session_id"])
    await runtime.context.services.begin_turn(state["user_message"], thread_id)
    constraints = build_composite_plan(state["user_message"]).constraints
    # react 等子图按 messages 驱动，父图必须先播种，否则子图读到 KeyError。
    #
    # 两件必须做的事：
    # 1. 本轮用户消息一定要接上。检查点会把上一轮的 messages 带回来，
    #    以前只在"没有 messages"时才塞本轮输入，于是第二轮起模型看到的是
    #    上一轮的问题，本轮这句根本没进上下文。
    # 2. 历史里"带 tool_calls 但没有 reasoning_content"的 assistant 消息要丢掉，
    #    否则 thinking 模型直接 400；它对应的 tool 消息一并丢弃，避免孤儿。
    messages = _sanitize_history(list(state.get("messages") or []))
    messages.append(HumanMessage(content=state["user_message"]))
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
    """多天行程规划：travel 与 trip_gear 共用同一套面板，只是装备段不同。"""
    return (
        str(classification.get("intent") or "") == "travel"
        and str(classification.get("sub_intent") or "") == "plan_trip"
        and str(classification.get("route") or "") == "plan_and_execute"
    )


ROUTE_KEYS = {"shopping", "travel", "trip_gear", "general", "customer_service"}


def _all_agent_ids() -> set:
    try:
        from app.agents.specs import AGENT_SPECS

        return set(AGENT_SPECS)
    except Exception:
        return set()


def _installed_agent_for_intent(intent: str) -> str:
    try:
        from app.agents.specs import agent_for_intent

        return agent_for_intent(intent)
    except Exception:
        return ""

# 出现这些词说明用户要的是"行程 + 装备协同"，不只是排行程。
# 词表迟早要并进 nlu/lexicon.py；先放在这里，是因为它决定的是路由而不是解析。
_GEAR_SCENE_HINTS = (
    "露营", "扎营", "装备", "清单", "带什么", "带啥", "徒步", "穿越", "登山",
)


def _wants_gear(message: str) -> bool:
    text = str(message or "")
    return any(term in text for term in _GEAR_SCENE_HINTS)


def _route_target(state: TurnState) -> str:
    """条件边：把分类结果翻译成下一个节点名。

    口径：trip_gear = 行程 + 装备（购物能力在其中），travel = 同一张图剥掉装备段。
    """
    forced = str(state.get("forced_agent") or "").strip().lower()
    if forced in ROUTE_KEYS or forced in _all_agent_ids():
        # MCP 的 run 工具 / 导入的 Agent：点名优先，分类照跑（留证据）。
        return forced

    classification = state.get("classification") or {}
    intent = classification.get("intent", "general")
    route_name = classification.get("route", "react")
    intents = set(classification.get("intents") or [intent])
    message = str(state.get("user_message") or "")

    if _is_gear_plan(classification) or _wants_gear(message):
        return "trip_gear"
    # 购物 + 旅行同时就绪：要排行程就由 trip_gear 一次算完（它本来就含商品检索）；
    # 旅行那半只是顺口一问（"顺便问下三亚天气"）时按主意图走，不强行拉成行程。
    if {"shopping", "travel"}.issubset(intents):
        if route_name == "plan_and_execute" or _wants_gear(message):
            return "trip_gear"
        return "shopping" if intent == "shopping" else "travel"
    if _is_trip_plan(classification) and intents <= {"travel"}:
        return "travel"
    if intent == "shopping":
        return "shopping"
    if intent == "customer_service":
        return "customer_service"
    if intent == "travel":
        return "travel"
    # 导入的 Agent 可以声明自己的意图入口（AgentSpec.intent）
    installed = _installed_agent_for_intent(intent)
    if installed:
        return installed
    return "general"


def route(state: TurnState) -> str:
    """入口兜底：被禁用的 Agent 不接客，回退到 general。"""
    target = _route_target(state)
    try:
        from app.agents.config import is_enabled

        if target in ROUTE_KEYS and not is_enabled(target):
            return "general"
    except Exception:
        return target
    return target


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
