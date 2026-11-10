"""通用 Agent 子图：ReAct 工具循环（纯 LangGraph）。

相比旧实现：不再由外部注入 ``on_event`` 回调、不再每轮 new 一个图实例。
模型与工具从 ``runtime.context.services`` 取，事件直接写 custom stream。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from app.execution_log import build_execution_log
from app.graphs.context import TurnContext
from app.graphs.state import GeneralState
from app.observability import extract_model_usage, record_trace, timed_span
from app.reliability import RetryableError
from app.tools.agent_capabilities import validate_agent_tools

DEFAULT_MAX_STEPS = 6
DEFAULT_TOOL_TIMEOUT = 10.0

AGENT_RETRY = RetryPolicy(
    max_attempts=2,
    initial_interval=0.05,
    retry_on=(RetryableError, ConnectionError, TimeoutError),
)


def _tool_calls(message: Any) -> List[Dict[str, Any]]:
    return list(getattr(message, "tool_calls", None) or [])


async def _log(sink, node: str, kind: str, message: str, **kwargs: Any) -> None:
    payload = build_execution_log(
        graph="general",
        node=node,
        kind=kind,
        message=message,
        step="general",
        **kwargs,
    )
    await sink.emit_execution_log(payload)


TOOL_USE_RULES = (
    "工具用法：只查你现在确实不知道的实时事实（天气、空气、汇率、节假日、路线、时间），"
    "能直接回答的不要查；查不到就说查不到，不拿常识补数字；"
    "不要把工具结果拼成多天行程或装备清单，那是行程规划链路的职责。"
)


def _with_tool_rules(messages, tools):
    """绑了工具才加用法约束；已经有系统消息就不重复加。"""
    if not tools or (messages and isinstance(messages[0], SystemMessage)):
        return list(messages)
    return [SystemMessage(content=TOOL_USE_RULES)] + list(messages)


def _tools_for(runtime: Runtime[TurnContext]):
    services = runtime.context.services
    actor_id = runtime.context.extra.get("actor_id") or ""
    agent_id = runtime.context.extra.get("agent_id") or "general"
    return services.tools_for(actor_id, agent_id)


def _policy_for(runtime: Runtime[TurnContext], tools):
    """只有显式声明 agent_id 时才做工具越权校验。"""
    agent_id = runtime.context.extra.get("agent_id")
    return validate_agent_tools(agent_id, tools) if agent_id else None


async def agent(state: GeneralState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    max_steps = state.get("max_steps") or DEFAULT_MAX_STEPS
    tools = _tools_for(runtime)
    model = runtime.context.services.llm
    model_with_tools = model.bind_tools(tools) if hasattr(model, "bind_tools") else model
    messages = _with_tool_rules(state["messages"], tools)

    await sink.emit("agent_started", {"step": state.get("step_count", 0) + 1}, step="general")
    with timed_span(
        "model.react_agent",
        trace_id=state.get("trace_id", ""),
        attributes={"step": state.get("step_count", 0) + 1},
    ) as measurement:
        response = await model_with_tools.ainvoke(messages)
        measurement.add_attributes(extract_model_usage(response))

    step_count = state.get("step_count", 0) + 1
    calls = _tool_calls(response)
    if calls and step_count < max_steps:
        branch, decision = "tools", f"模型请求调用 {len(calls)} 个工具，进入工具分支"
    else:
        branch = "finalize"
        decision = (
            f"达到工具循环上限 {max_steps}，强制收束"
            if calls else "模型未请求工具，进入最终回答分支"
        )
    await _log(
        sink,
        "should_continue",
        "branch_selected",
        decision,
        status="warning" if calls and step_count >= max_steps else "ok",
        branch=branch,
        iteration=step_count,
        max_iterations=max_steps,
        details={"tool_calls": [call.get("name", "") for call in calls]},
    )
    return {
        "messages": list(state["messages"]) + [response],
        "step_count": step_count,
        "status": "agent_completed",
    }


def should_continue(state: GeneralState) -> str:
    calls = _tool_calls(state["messages"][-1])
    if not calls:
        return "finalize"
    if state.get("step_count", 0) >= (state.get("max_steps") or DEFAULT_MAX_STEPS):
        return "finalize"
    return "tools"


async def call_tool(
    runtime: Runtime[TurnContext],
    tools_by_name: Dict[str, Any],
    policy: Optional[Any],
    call: Dict[str, Any],
    trace_id: str,
    tool_timeout: float,
) -> Dict[str, Any]:
    name = call.get("name", "")
    started = time.monotonic()

    if policy is not None and not policy.allows(name):
        error = f"工具越权: {name} 不属于 Agent {policy.agent_id}，已拒绝执行"
        record_trace(
            f"tool.{name or 'unauthorized'}", 0.0, trace_id=trace_id,
            status="error", attributes={"error": error},
        )
        return {"tool": name, "ok": False, "result": "", "error": error, "duration_ms": 0}

    tool = tools_by_name.get(name)
    if tool is None:
        error = f"未知工具: {name}"
        record_trace(
            f"tool.{name or 'unknown'}", 0.0, trace_id=trace_id,
            status="error", attributes={"error": error},
        )
        return {"tool": name, "ok": False, "result": "", "error": error, "duration_ms": 0}

    try:
        args = call.get("args", {}) or {}
        if hasattr(tool, "ainvoke"):
            result = await asyncio.wait_for(tool.ainvoke(args), timeout=tool_timeout)
        elif hasattr(tool, "invoke"):
            result = await asyncio.wait_for(asyncio.to_thread(tool.invoke, args), timeout=tool_timeout)
        else:
            result = tool(**args)
        duration_ms = int((time.monotonic() - started) * 1000)
        record_trace(f"tool.{name}", duration_ms, trace_id=trace_id, attributes={"ok": True})
        return {"tool": name, "ok": True, "result": result, "error": "", "duration_ms": duration_ms}
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        record_trace(
            f"tool.{name}", duration_ms, trace_id=trace_id,
            status="error", attributes={"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
        )
        return {
            "tool": name,
            "ok": False,
            "result": "",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "duration_ms": duration_ms,
        }


async def tools_node(state: GeneralState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    max_steps = state.get("max_steps") or DEFAULT_MAX_STEPS
    tool_timeout = state.get("tool_timeout") or DEFAULT_TOOL_TIMEOUT
    tool_items = _tools_for(runtime)
    tools_by_name = {item.name: item for item in tool_items}
    policy = _policy_for(runtime, tool_items)

    calls = _tool_calls(state["messages"][-1])
    messages = list(state["messages"])
    results = list(state.get("tool_results", []))
    for index, call in enumerate(calls, start=1):
        await _log(
            sink, "tools", "loop_iteration",
            f"执行第 {index}/{len(calls)} 个本批工具调用",
            status="running",
            iteration=state.get("step_count", 0),
            max_iterations=max_steps,
            details={"tool": call.get("name", "")},
        )
        await sink.emit_tool_call(call.get("name", ""), call.get("args", {}), step="general")
        outcome = await call_tool(
            runtime, tools_by_name, policy, call, state.get("trace_id", ""), tool_timeout
        )
        await _log(
            sink, "tools",
            "tool_completed" if outcome.get("ok") else "tool_failed",
            (
                f"工具 {outcome.get('tool', '')} 执行成功"
                if outcome.get("ok")
                else f"工具 {outcome.get('tool', '')} 执行失败：{outcome.get('error', '')}"
            ),
            status="ok" if outcome.get("ok") else "error",
            iteration=state.get("step_count", 0),
            max_iterations=max_steps,
            details={"tool": outcome.get("tool", ""), "error": outcome.get("error", "")},
        )
        results.append(outcome)
        if outcome.get("ok"):
            await sink.emit_tool_result(outcome["tool"], str(outcome["result"]), step="general")
        else:
            await sink.emit_error(
                f"工具 {outcome.get('tool', '')} 执行失败: {outcome.get('error', '')}",
                step="general",
            )
        content = outcome["result"] if outcome["ok"] else {
            "error": outcome["error"], "tool": outcome["tool"],
        }
        messages.append(ToolMessage(
            content=json.dumps(content, ensure_ascii=False, default=str),
            tool_call_id=call.get("id", f"call-{len(results)}"),
            name=call.get("name", ""),
        ))
    return {"messages": messages, "tool_results": results, "status": "tools_completed"}


async def finalize(state: GeneralState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    max_steps = state.get("max_steps") or DEFAULT_MAX_STEPS
    calls = _tool_calls(state["messages"][-1])
    if calls:
        status = "max_steps_reached"
        error = f"达到最大工具调用步数 {max_steps}"
    else:
        status, error = "completed", ""
    await sink.emit("finalized", {"status": status, "step_count": state.get("step_count", 0)}, step="general")
    await _log(
        sink, "finalize",
        "limit_reached" if status == "max_steps_reached" else "completed",
        error or f"工具循环完成，共执行 {state.get('step_count', 0)} 步",
        status="warning" if status == "max_steps_reached" else "ok",
        branch="finalize",
        iteration=state.get("step_count", 0),
        max_iterations=max_steps,
        details={"status": status},
    )
    response = ""
    for message in reversed(state.get("messages", [])):
        if isinstance(message, AIMessage) and not _tool_calls(message):
            response = message.content
            break
    try:
        from app.ui.blocks import text_only

        await sink.emit("answer_blocks", {
            "agent": "general",
            "blocks": text_only(str(response or "")),
        }, step="general")
    except Exception:
        pass
    return {"status": status, "error": error, "response": response}


def build_general_graph(checkpointer: Optional[Any] = None):
    graph = StateGraph(GeneralState, context_schema=TurnContext)
    graph.add_node("agent", agent, retry_policy=AGENT_RETRY, timeout=60)
    graph.add_node("tools", tools_node, timeout=DEFAULT_MAX_STEPS * DEFAULT_TOOL_TIMEOUT + 5)
    graph.add_node("finalize", finalize, timeout=5)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "finalize": "finalize"})
    graph.add_edge("tools", "agent")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def initial_general_state(
    messages: List[Any],
    trace_id: str = "general",
    max_steps: int = DEFAULT_MAX_STEPS,
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT,
) -> Dict[str, Any]:
    return {
        "messages": list(messages or [HumanMessage(content="")]),
        "tool_results": [],
        "step_count": 0,
        "max_steps": max_steps,
        "tool_timeout": tool_timeout,
        "status": "started",
        "error": "",
        "trace_id": trace_id,
    }
