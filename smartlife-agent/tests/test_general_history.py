"""多轮：本轮用户消息必须进上下文，历史里的工具消息必须能安全重放。"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.graphs.nodes import _sanitize_history


def test_assistant_tool_calls_without_reasoning_are_dropped_with_their_results():
    """thinking 模型会因为"带 tool_calls 却缺 reasoning_content"直接 400。"""
    assistant = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}])
    messages = [
        HumanMessage(content="今天天气咋样"),
        assistant,
        ToolMessage(content="晴", tool_call_id="c1"),
        AIMessage(content="上海今天晴"),
    ]

    kept = _sanitize_history(messages)

    assert [m.type for m in kept] == ["human", "ai"]
    assert kept[1].content == "上海今天晴"


def test_assistant_tool_calls_with_reasoning_survive():
    assistant = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c2"}])
    assistant.additional_kwargs["reasoning_content"] = "先查一下"
    messages = [
        HumanMessage(content="天气"),
        assistant,
        ToolMessage(content="晴", tool_call_id="c2"),
    ]

    kept = _sanitize_history(messages)

    assert [m.type for m in kept] == ["human", "ai", "tool"]


def test_prepare_appends_the_current_message_to_carried_history():
    """检查点会带回上一轮的 messages；本轮这句必须接在后面。"""
    import asyncio
    from types import SimpleNamespace

    from app.graphs.context import TurnContext
    from app.graphs.nodes import prepare

    class _Services:
        async def begin_turn(self, message, thread_id):
            return None

    old = AIMessage(content="上一轮的回答")
    state = {
        "user_id": "u",
        "session_id": "s",
        "scene": "general",
        "user_message": "上海",
        "messages": [HumanMessage(content="今天天气咋样"), old],
    }
    runtime = SimpleNamespace(context=TurnContext(services=_Services()))

    result = asyncio.run(prepare(state, runtime))

    contents = [getattr(m, "content", "") for m in result["messages"]]
    assert contents[-1] == "上海"
    assert contents[-2] == "上一轮的回答"
