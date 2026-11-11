"""thinking 模型：reasoning_content 必须原样回传。

回归背景：ChatOpenAI 不解析第三方推理字段，工具循环第二轮直接 400：
"The `reasoning_content` in the thinking mode must be passed back to the API."
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.config import ReasoningChatOpenAI


def _model():
    return ReasoningChatOpenAI(model="x", api_key="k", base_url="http://example.invalid/v1")


def test_reasoning_content_is_captured_from_the_response():
    raw = {
        "choices": [
            {"message": {"role": "assistant", "content": "你好", "reasoning_content": "先想一想"}}
        ]
    }

    result = _model()._create_chat_result(raw)

    assert result.generations[0].message.additional_kwargs["reasoning_content"] == "先想一想"


def test_reasoning_content_is_sent_back_with_the_assistant_message():
    assistant = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}])
    assistant.additional_kwargs["reasoning_content"] = "先查工具"

    payload = _model()._get_request_payload([
        HumanMessage(content="查一下"),
        assistant,
        ToolMessage(content="结果", tool_call_id="c1"),
    ])

    assistant_payload = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert assistant_payload["reasoning_content"] == "先查工具"


def test_messages_without_reasoning_are_untouched():
    payload = _model()._get_request_payload([HumanMessage(content="你好")])

    assert "reasoning_content" not in payload["messages"][0]


def test_tool_turns_are_dropped_for_the_fallback_call():
    """拿不到 reasoning 时的兜底：丢掉带 tool_calls 的 assistant 和 tool 消息。"""
    from app.graphs.subgraphs.general import _without_tool_turns

    assistant = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}])
    messages = [
        HumanMessage(content="上海天气"),
        assistant,
        ToolMessage(content="晴", tool_call_id="c1"),
        AIMessage(content="上海今天晴"),
    ]

    kept = _without_tool_turns(messages)

    assert [m.type for m in kept] == ["human", "ai"]
    assert kept[1].content == "上海今天晴"
