"""通用对话子图：单节点流式生成，历史与 prompt 由服务层提供。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.graphs.context import TurnContext
from app.graphs.state import GeneralState
from app.observability import extract_model_usage, timed_span
from app.streaming import EventType


async def generate(state: GeneralState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    services = runtime.context.services
    history = state.get("history") or services.get_chat_history(state.get("thread_id", ""))

    await sink.emit(EventType.STEP, {
        "step": 2, "total": 4,
        "name": "通用对话",
        "description": "处理通用对话请求",
    }, step="general")

    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是 SmartLife Agent 智能助手，帮助用户购物和旅行规划。用中文回复。友好、专业、简洁。"),
        *[(msg.role, msg.content) for msg in list(history)[-5:]],
        ("user", state["user_message"]),
    ])
    chain = prompt | services.llm
    full_response = ""
    with timed_span("model.general_generate", trace_id=runtime.context.trace_id) as measurement:
        async for chunk in chain.astream({}):
            if chunk.content:
                full_response += chunk.content
                await sink.emit_token(chunk.content, step="general")
            measurement.add_attributes(extract_model_usage(chunk))
    return {"response": full_response, "status": "generated"}


def build_general_chat_graph(checkpointer: Optional[Any] = None):
    graph = StateGraph(GeneralState, context_schema=TurnContext)
    graph.add_node("generate", generate, timeout=120)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", END)
    return graph.compile(checkpointer=checkpointer)
