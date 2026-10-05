"""流式过程事件按逻辑节点去重测试。"""

from app.stream_response import StreamResponseBuffer
from app.streaming import EventType, deduplicate_process_events


def test_duplicate_steps_and_classification_are_rendered_once():
    events = [
        {"event": "step", "data": {"step": 1, "total": 4, "name": "统一分类"}},
        {"event": "step", "data": {"step": 1, "total": 4, "name": "统一分类"}},
        {"event": "step", "data": {"step": 2, "total": 4, "name": "旅游资料检索"}},
        {"event": "step", "data": {"step": 3, "total": 4, "name": "生成并优化行程"}},
        {"event": "classify", "data": {"intent": "shopping"}, "step": "classify"},
        {"event": "classify", "data": {"intent": "travel"}, "step": "classify"},
        {
            "event": "tool_call",
            "data": {"tool": "rag", "input": "返回 2 条文档"},
            "step": "retrieval",
        },
        {
            "event": "tool_call",
            "data": {"tool": "rag", "input": "返回 2 条文档"},
            "step": "retrieval",
        },
    ]

    result = deduplicate_process_events(events)

    assert [event["event"] for event in result] == [
        "step", "step", "step", "classify", "tool_call"
    ]
    assert [event["data"]["step"] for event in result[:3]] == [1, 2, 3]
    assert result[3]["data"]["intent"] == "travel"


def test_different_tool_calls_and_thinking_events_are_preserved():
    events = [
        {"event": "thinking", "data": {"message": "开始检索"}},
        {"event": "thinking", "data": {"message": "开始生成"}},
        {
            "event": "tool_call",
            "data": {"tool": "nl2sql", "input": "SELECT * FROM products"},
            "step": "retrieval",
        },
        {
            "event": "tool_call",
            "data": {"tool": "rag", "input": "返回 1 条文档"},
            "step": "retrieval",
        },
    ]

    assert deduplicate_process_events(events) == events


def test_revised_response_replaces_initial_response_instead_of_appending():
    response = StreamResponseBuffer()

    response.handle_event({"event": EventType.TOKEN, "data": {"token": "一、二、三、四、五、六"}})
    response.handle_event({"event": EventType.RESPONSE_RESET, "data": {}})
    response.handle_event({"event": EventType.TOKEN, "data": {"token": "修订版：一、二、三、四、五、六"}})
    response.handle_event({
        "event": EventType.DONE,
        "data": {"response": "修订版：一、二、三、四、五、六"},
    })

    assert response.text == "修订版：一、二、三、四、五、六"


def test_done_response_is_authoritative_over_accumulated_tokens():
    response = StreamResponseBuffer()

    response.handle_event({"event": EventType.TOKEN, "data": {"token": "初版正文"}})
    response.handle_event({"event": EventType.DONE, "data": {"response": "最终正文"}})

    assert response.text == "最终正文"
