"""
受控的 Tool-Calling ReAct 子图。

图结构：
    agent -> should_continue -> tools -> agent
                         \\-> finalize

工具调用统一执行、超时、错误转换和步数限制，避免模型无限循环或把工具
异常直接抛给用户。
"""
import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from app.execution_log import CallbackEventQueue, build_execution_log, run_policy_node
from app.observability import extract_model_usage, record_trace, timed_span
from app.reliability import NodePolicy, RetryPolicy, RetryableError


class ReActState(TypedDict):
    messages: List[Any]
    tool_results: List[Dict[str, Any]]
    step_count: int
    max_steps: int
    status: str
    error: str
    trace_id: str


class ToolReActGraph:
    """带预算、超时和错误处理的 LangGraph ReAct 执行器。"""

    def __init__(
        self,
        model: Any,
        tools: Sequence[Any],
        max_steps: int = 6,
        tool_timeout: float = 10.0,
        on_event: Optional[Callable[..., Any]] = None,
    ):
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")
        if tool_timeout <= 0:
            raise ValueError("tool_timeout 必须大于 0")
        self.tools = list(tools or [])
        self.tools_by_name = {tool.name: tool for tool in self.tools}
        self.max_steps = max_steps
        self.tool_timeout = tool_timeout
        self.on_event = on_event
        self.model = model
        self.model_with_tools = (
            model.bind_tools(self.tools) if hasattr(model, "bind_tools") else model
        )
        self.node_policies = {
            "agent": NodePolicy(
                timeout_seconds=60,
                retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.05),
                retryable_exceptions=(RetryableError, ConnectionError, TimeoutError),
            ),
            "tools": NodePolicy(timeout_seconds=max_steps * tool_timeout + 5),
            "finalize": NodePolicy(timeout_seconds=5),
        }
        self.graph = self._build_graph()

    def _policy_node(self, name: str, node):
        policy = self.node_policies[name]

        async def run(state: ReActState):
            async def operation(_attempt: int):
                return await node(state)
            return await run_policy_node(
                policy,
                operation,
                queue=CallbackEventQueue(self._emit),
                graph="react",
                node=name,
                step="react",
                details={"step_count": state.get("step_count", 0)},
            )

        return run

    async def _log(
        self,
        node: str,
        kind: str,
        message: str,
        *,
        status: str = "info",
        branch: Optional[str] = None,
        iteration: Optional[int] = None,
        max_iterations: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = build_execution_log(
            graph="react",
            node=node,
            kind=kind,
            message=message,
            status=status,
            step="react",
            branch=branch,
            iteration=iteration,
            max_iterations=max_iterations,
            details=details,
        )
        await self._emit(payload["event"], payload["data"])

    async def _emit(self, event: str, data: Dict[str, Any]):
        if self.on_event:
            result = self.on_event(event, data)
            if hasattr(result, "__await__"):
                await result

    async def _agent(self, state: ReActState) -> Dict[str, Any]:
        await self._emit("agent_started", {"step": state.get("step_count", 0) + 1})
        with timed_span(
            "model.react_agent",
            trace_id=state.get("trace_id", ""),
            attributes={"step": state.get("step_count", 0) + 1},
        ) as measurement:
            response = await self.model_with_tools.ainvoke(state["messages"])
            measurement.add_attributes(extract_model_usage(response))
        step_count = state.get("step_count", 0) + 1
        calls = self._tool_calls(response)
        if calls and step_count < self.max_steps:
            branch = "tools"
            decision = f"模型请求调用 {len(calls)} 个工具，进入工具分支"
        else:
            branch = "finalize"
            decision = (
                f"达到工具循环上限 {self.max_steps}，强制收束"
                if calls else "模型未请求工具，进入最终回答分支"
            )
        await self._log(
            "should_continue",
            "branch_selected",
            decision,
            status="warning" if calls and step_count >= self.max_steps else "ok",
            branch=branch,
            iteration=step_count,
            max_iterations=self.max_steps,
            details={"tool_calls": [call.get("name", "") for call in calls]},
        )
        return {
            "messages": list(state["messages"]) + [response],
            "step_count": step_count,
            "status": "agent_completed",
        }

    @staticmethod
    def _tool_calls(message: Any) -> List[Dict[str, Any]]:
        return list(getattr(message, "tool_calls", None) or [])

    def _should_continue(self, state: ReActState) -> str:
        calls = self._tool_calls(state["messages"][-1])
        if not calls:
            return "finalize"
        if state.get("step_count", 0) >= self.max_steps:
            return "finalize"
        return "tools"

    async def _call_tool(
        self,
        call: Dict[str, Any],
        trace_id: str = "",
    ) -> Dict[str, Any]:
        name = call.get("name", "")
        tool = self.tools_by_name.get(name)
        started = time.monotonic()
        if tool is None:
            result = {
                "tool": name,
                "ok": False,
                "result": "",
                "error": f"未知工具: {name}",
                "duration_ms": 0,
            }
            record_trace(
                f"tool.{name or 'unknown'}",
                0.0,
                trace_id=trace_id,
                status="error",
                attributes={"error": result["error"]},
            )
            return result
        try:
            args = call.get("args", {}) or {}
            if hasattr(tool, "ainvoke"):
                result = await asyncio.wait_for(tool.ainvoke(args), timeout=self.tool_timeout)
            elif hasattr(tool, "invoke"):
                result = await asyncio.wait_for(
                    asyncio.to_thread(tool.invoke, args),
                    timeout=self.tool_timeout,
                )
            else:
                result = tool(**args)
            duration_ms = int((time.monotonic() - started) * 1000)
            record_trace(
                f"tool.{name}",
                duration_ms,
                trace_id=trace_id,
                attributes={"ok": True},
            )
            return {
                "tool": name,
                "ok": True,
                "result": result,
                "error": "",
                "duration_ms": duration_ms,
            }
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            record_trace(
                f"tool.{name}",
                duration_ms,
                trace_id=trace_id,
                status="error",
                attributes={"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
            return {
                "tool": name,
                "ok": False,
                "result": "",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "duration_ms": duration_ms,
            }

    async def _tools(self, state: ReActState) -> Dict[str, Any]:
        calls = self._tool_calls(state["messages"][-1])
        messages = list(state["messages"])
        results = list(state.get("tool_results", []))
        for index, call in enumerate(calls, start=1):
            await self._log(
                "tools",
                "loop_iteration",
                f"执行第 {index}/{len(calls)} 个本批工具调用",
                status="running",
                iteration=state.get("step_count", 0),
                max_iterations=self.max_steps,
                details={"tool": call.get("name", "")},
            )
            await self._emit("tool_call", {"tool": call.get("name", ""), "args": call.get("args", {})})
            outcome = await self._call_tool(call, trace_id=state.get("trace_id", ""))
            await self._log(
                "tools",
                "tool_completed" if outcome.get("ok") else "tool_failed",
                (
                    f"工具 {outcome.get('tool', '')} 执行成功"
                    if outcome.get("ok")
                    else f"工具 {outcome.get('tool', '')} 执行失败：{outcome.get('error', '')}"
                ),
                status="ok" if outcome.get("ok") else "error",
                iteration=state.get("step_count", 0),
                max_iterations=self.max_steps,
                details={"tool": outcome.get("tool", ""), "error": outcome.get("error", "")},
            )
            results.append(outcome)
            await self._emit("tool_result", outcome)
            content = outcome["result"] if outcome["ok"] else {
                "error": outcome["error"],
                "tool": outcome["tool"],
            }
            messages.append(ToolMessage(
                content=json.dumps(content, ensure_ascii=False, default=str),
                tool_call_id=call.get("id", f"call-{len(results)}"),
                name=call.get("name", ""),
            ))
        return {
            "messages": messages,
            "tool_results": results,
            "status": "tools_completed",
        }

    async def _finalize(self, state: ReActState) -> Dict[str, Any]:
        calls = self._tool_calls(state["messages"][-1])
        if calls:
            status = "max_steps_reached"
            error = f"达到最大工具调用步数 {self.max_steps}"
        else:
            status = "completed"
            error = ""
        await self._emit("finalized", {"status": status, "step_count": state.get("step_count", 0)})
        await self._log(
            "finalize",
            "limit_reached" if status == "max_steps_reached" else "completed",
            error or f"工具循环完成，共执行 {state.get('step_count', 0)} 步",
            status="warning" if status == "max_steps_reached" else "ok",
            branch="finalize",
            iteration=state.get("step_count", 0),
            max_iterations=self.max_steps,
            details={"status": status},
        )
        return {"status": status, "error": error}

    def _build_graph(self):
        graph = StateGraph(ReActState)
        graph.add_node("agent", self._policy_node("agent", self._agent))
        graph.add_node("tools", self._policy_node("tools", self._tools))
        graph.add_node("finalize", self._policy_node("finalize", self._finalize))
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent",
            self._should_continue,
            {"tools": "tools", "finalize": "finalize"},
        )
        graph.add_edge("tools", "agent")
        graph.add_edge("finalize", END)
        return graph.compile()

    async def run_streaming(
        self,
        messages: Sequence[Any],
        thread_id: str = "react",
    ) -> Dict[str, Any]:
        initial: ReActState = {
            "messages": list(messages or [HumanMessage(content="")]),
            "tool_results": [],
            "step_count": 0,
            "max_steps": self.max_steps,
            "status": "started",
            "error": "",
            "trace_id": thread_id,
        }
        result = await self.graph.ainvoke(
            initial,
            config={"configurable": {"thread_id": thread_id}},
        )
        response = ""
        for message in reversed(result.get("messages", [])):
            if isinstance(message, AIMessage) and not self._tool_calls(message):
                response = message.content
                break
        return {
            **result,
            "response": response,
            "tool_count": len(result.get("tool_results", [])),
        }

    def run(self, messages: Sequence[Any], thread_id: str = "react") -> Dict[str, Any]:
        return asyncio.run(self.run_streaming(messages, thread_id))
