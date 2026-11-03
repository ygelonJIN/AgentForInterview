"""Tool-Calling ReAct 循环、超时和预算测试。"""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from app.graphs.context import TurnContext
from app.graphs.subgraphs.react import build_react_graph, initial_react_state
from app.observability import get_trace_recorder


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


class _Services:
    def __init__(self, model, tools):
        self.llm = model
        self._tools = list(tools)

    def tools_for(self, actor_id, agent_id="general"):
        return self._tools


def _run(model, tools, *, max_steps=4, tool_timeout=1.0, agent_id=None, trace_id="react"):
    extra = {}
    if agent_id:
        extra["agent_id"] = agent_id
    ctx = TurnContext(services=_Services(model, tools), trace_id=trace_id).with_extra(**extra)
    state = initial_react_state(
        [HumanMessage(content="查询回显")],
        trace_id=trace_id,
        max_steps=max_steps,
        tool_timeout=tool_timeout,
    )
    return asyncio.run(build_react_graph().ainvoke(state, context=ctx))


def test_react_graph_runs_tool_then_returns_final_answer():
    tool = _Tool()
    model = _Model()

    result = _run(model, [tool])

    assert result["response"] == "最终回答"
    assert result["status"] == "completed"
    assert len(result["tool_results"]) == 1
    assert result["tool_results"][0]["tool"] == "echo"
    assert result["tool_results"][0]["ok"] is True
    assert tool.calls == [{"value": 1}]


def test_react_graph_stops_at_max_steps():
    model = _Model(always_call=True)
    result = _run(model, [_Tool()], max_steps=2)

    assert result["status"] == "max_steps_reached"
    assert result["step_count"] == 2
    # 第一次 agent 决定调用工具，第二次 agent 到达预算后直接收束。
    assert len(result["tool_results"]) == 1
    assert result["error"]


def test_react_graph_turns_tool_timeout_into_tool_error():
    slow_tool = _Tool(delay=0.05)
    model = _Model()
    result = _run(model, [slow_tool], tool_timeout=0.001)

    assert result["response"] == "最终回答"
    assert result["status"] == "completed"
    assert result["tool_results"][0]["ok"] is False
    assert "TimeoutError" in result["tool_results"][0]["error"]


def test_react_graph_reports_unknown_tool_without_crashing():
    model = _Model(tool_name="missing")
    result = _run(model, [_Tool()])

    assert result["tool_results"][0]["ok"] is False
    assert "未知工具" in result["tool_results"][0]["error"]


def test_react_graph_records_model_and_tool_trace_spans():
    recorder = get_trace_recorder()
    recorder.clear()
    _run(_Model(), [_Tool()], trace_id="trace-react")

    spans = recorder.summary()["spans"]
    assert spans["model.react_agent"]["count"] == 2
    assert spans["tool.echo"]["count"] == 1
