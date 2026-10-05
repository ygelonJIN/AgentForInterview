"""Tool-Calling ReAct 循环、超时和预算测试。"""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from app.agents.react_graph import ToolReActGraph


class _Tool:
    def __init__(self, name="echo", delay=0.0):
        self.name = name
        self.delay = delay
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"echo": args}


class _Model:
    def __init__(self, tool_name="echo", always_call=False):
        self.tool_name = tool_name
        self.always_call = always_call
        self.invocations = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, messages):
        self.invocations += 1
        already_called = any(isinstance(message, AIMessage) for message in messages)
        if self.always_call or not already_called:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": self.tool_name,
                    "args": {"value": self.invocations},
                    "id": f"call-{self.invocations}",
                }],
            )
        return AIMessage(content="最终回答")


def test_react_graph_runs_tool_then_returns_final_answer():
    tool = _Tool()
    model = _Model()
    graph = ToolReActGraph(model, [tool], max_steps=4, tool_timeout=1.0)

    result = asyncio.run(graph.run_streaming([HumanMessage(content="查询回显")]))

    assert result["response"] == "最终回答"
    assert result["status"] == "completed"
    assert result["tool_count"] == 1
    assert result["tool_results"][0]["tool"] == "echo"
    assert result["tool_results"][0]["ok"] is True
    assert tool.calls == [{"value": 1}]


def test_react_graph_stops_at_max_steps():
    model = _Model(always_call=True)
    graph = ToolReActGraph(model, [_Tool()], max_steps=2, tool_timeout=1.0)

    result = asyncio.run(graph.run_streaming([HumanMessage(content="无限调用")]))

    assert result["status"] == "max_steps_reached"
    assert result["step_count"] == 2
    # 第一次 agent 决定调用工具，第二次 agent 到达预算后直接收束。
    assert result["tool_count"] == 1
    assert result["error"]


def test_react_graph_turns_tool_timeout_into_tool_error():
    slow_tool = _Tool(delay=0.05)
    model = _Model()
    graph = ToolReActGraph(model, [slow_tool], max_steps=4, tool_timeout=0.001)

    result = asyncio.run(graph.run_streaming([HumanMessage(content="调用慢工具")]))

    assert result["response"] == "最终回答"
    assert result["status"] == "completed"
    assert result["tool_results"][0]["ok"] is False
    assert "TimeoutError" in result["tool_results"][0]["error"]


def test_react_graph_reports_unknown_tool_without_crashing():
    model = _Model(tool_name="missing")
    graph = ToolReActGraph(model, [_Tool()], max_steps=4, tool_timeout=1.0)

    result = asyncio.run(graph.run_streaming([HumanMessage(content="调用不存在的工具")]))

    assert result["tool_results"][0]["ok"] is False
    assert "未知工具" in result["tool_results"][0]["error"]
