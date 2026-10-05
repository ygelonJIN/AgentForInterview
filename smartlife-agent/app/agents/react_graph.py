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


class ReActState(TypedDict):
    messages: List[Any]
    tool_results: List[Dict[str, Any]]
    step_count: int
    max_steps: int
    status: str
    error: str


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
        self.graph = self._build_graph()

    async def _emit(self, event: str, data: Dict[str, Any]):
        if self.on_event:
            result = self.on_event(event, data)
            if hasattr(result, "__await__"):
                await result

    async def _agent(self, state: ReActState) -> Dict[str, Any]:
        await self._emit("agent_started", {"step": state.get("step_count", 0) + 1})
        response = await self.model_with_tools.ainvoke(state["messages"])
        return {
            "messages": list(state["messages"]) + [response],
            "step_count": state.get("step_count", 0) + 1,
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

    async def _call_tool(self, call: Dict[str, Any]) -> Dict[str, Any]:
        name = call.get("name", "")
        tool = self.tools_by_name.get(name)
        started = time.monotonic()
        if tool is None:
            return {
                "tool": name,
                "ok": False,
                "result": "",
                "error": f"未知工具: {name}",
                "duration_ms": 0,
            }
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
            return {
                "tool": name,
                "ok": True,
                "result": result,
                "error": "",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            return {
                "tool": name,
                "ok": False,
                "result": "",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }

    async def _tools(self, state: ReActState) -> Dict[str, Any]:
        calls = self._tool_calls(state["messages"][-1])
        messages = list(state["messages"])
        results = list(state.get("tool_results", []))
        for call in calls:
            await self._emit("tool_call", {"tool": call.get("name", ""), "args": call.get("args", {})})
            outcome = await self._call_tool(call)
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
        return {"status": status, "error": error}

    def _build_graph(self):
        graph = StateGraph(ReActState)
        graph.add_node("agent", self._agent)
        graph.add_node("tools", self._tools)
        graph.add_node("finalize", self._finalize)
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
