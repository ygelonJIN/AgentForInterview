"""纯 LangGraph 顶层图的端到端回归。

约束（本文件的断言即架构契约）：
1. 入口只驱动编译后的图，不持有任何编排对象；
2. 流式走 ``astream(stream_mode="custom")``，事件来自 ``Runtime.stream_writer``；
3. 子图作为节点挂载，其 custom 事件透传到顶层；
4. 节点超时/重试由 LangGraph 原生参数承担。
"""
import asyncio
import json
from types import SimpleNamespace

from app.classifier import ClassificationResult
from app.graphs.orchestrator import TurnGraph, build_turn_graph


def _events(lines):
    return [json.loads(line.removeprefix("data: ")) for line in lines]


class _Memory:
    def __init__(self):
        self.messages = []

    def add_message(self, thread_id, role, content):
        self.messages.append((thread_id, role, content))

    def get_history(self, thread_id, last_n=None):
        return self.messages

    def get_langchain_messages(self, thread_id, last_n=10):
        return [SimpleNamespace(role=role, content=content) for _, role, content in self.messages]


def _fake_llm(text="你好"):
    """最小可流式 Runnable，供 general_chat 子图使用。"""
    from langchain_core.runnables import RunnableLambda

    return RunnableLambda(lambda _prompt: SimpleNamespace(content=text, tool_calls=[]))


class _FakeServices:
    """只实现图节点真正调用的能力。"""

    def __init__(self, intent="general", route="react", intents=None):
        self.intent = intent
        self.route = route
        self.intents = list(intents or [])
        self.short_term_memory = _Memory()
        self.classify_calls = []
        self.llm = _fake_llm()

    # --- 分类 ---
    async def classify(self, user_message, thread_id, queue):
        self.classify_calls.append((user_message, thread_id))
        result = ClassificationResult(
            route=self.route,
            intent=self.intent,
            intents=self.intents or [self.intent],
            sub_intent="chat",
            retrieval="none",
            confidence=1.0,
            reason="test",
        )
        await queue.emit("classify", {"intent": result.intent}, step="classify")
        return result

    def get_chat_history(self, thread_id):
        return []

    async def begin_turn(self, user_message, thread_id):
        self.short_term_memory.add_message(thread_id, "user", user_message)

    async def finish_turn(self, thread_id, response_text, classification, queue):
        self.short_term_memory.add_message(thread_id, "assistant", response_text)
        await queue.emit_done({"response": response_text})

    # --- 购物 ---
    async def plan_shopping_retrieval(self, user_message, classification, queue):
        return {"needs_product": True, "needs_review": False, "strategy": "sql_only"}, "sql_only"

    async def retrieve_shopping_context(self, user_message, strategy, needs, queue, user_id=None):
        return {"product_context": "跑鞋 399", "source_status": {"sql": "ok"}}

    async def generate_shopping_response(self, *args, **kwargs):
        queue = args[5]
        await queue.emit_token("购物", step="generate")
        return "购物推荐：跑鞋 399"

    def validate_shopping_response(self, response, retrieval):
        return None

    # --- 复合 ---
    async def synthesize_composite_response(self, user_message, shopping, travel, constraints, queue):
        return f"合并[{shopping.get('status')}/{travel.get('status')}]"

    # --- 记忆 ---
    def extract_candidate_memories_result(self, user_id, session_id, scene, excluded_contents=None):
        return SimpleNamespace(as_dict=lambda: {"candidates": [], "status": "no_content"})


def _run_graph(services, **overrides):
    graph = TurnGraph(services)
    payload = {
        "user_message": "推荐一双跑鞋",
        "user_id": "u1",
        "session_id": "s1",
        "scene": "general",
    }
    payload.update(overrides)

    async def run():
        return [line async for line in graph.process_streaming(
            payload["user_message"],
            payload["user_id"],
            payload["session_id"],
            scene=payload["scene"],
        )]

    return _events(asyncio.run(run()))


def test_general_chat_subgraph_streams_tokens_and_returns_response():
    from app.graphs.context import TurnContext
    from app.graphs.subgraphs.general_chat import build_general_chat_graph

    services = _FakeServices()
    collected = []
    ctx = TurnContext(services=services, trace_id="t1").bind_writer(collected.append)

    async def run():
        return await build_general_chat_graph().ainvoke(
            {"user_message": "你好", "thread_id": "th1", "status": "started"},
            context=ctx,
        )

    result = asyncio.run(run())
    assert result["response"] == "你好"
    assert any(event["event"] == "token" for event in collected)
    assert any(event["event"] == "step" for event in collected)


def test_shopping_subgraph_events_propagate_through_custom_stream():
    services = _FakeServices(intent="shopping", route="react")
    events = _run_graph(services)
    names = [event["event"] for event in events]
    assert "token" in names, "子图 token 必须透传到顶层 custom stream"
    assert any(
        event["event"] == "execution_log" and event["data"].get("graph") == "shopping"
        for event in events
    ), "子图执行日志必须带 graph=shopping"
    done = next(event for event in events if event["event"] == "done")
    assert "跑鞋" in done["data"]["response"]


def test_composite_intent_runs_parallel_branches_and_reduces():
    services = _FakeServices(intent="shopping", route="react", intents=["shopping", "travel"])
    # 复合分支里 travel 走服务桩
    services.travel_service = SimpleNamespace(
        plan_trip_streaming=_async_return("杭州两日"),
        chat_streaming=_async_return("杭州两日"),
    )
    events = _run_graph(services)
    done = next(event for event in events if event["event"] == "done")
    # 关键：购物分支的正文必须真的传进合成，不能再退回“分支缺失”默认值
    assert "合并[ok/" in done["data"]["response"]
    assert "failed/failed" not in done["data"]["response"]


def test_classification_is_recorded_in_state_not_in_orchestrator():
    services = _FakeServices(intent="shopping", route="react")
    _run_graph(services)
    assert services.classify_calls, "分类必须由 classify 节点调用服务完成"
    assert services.short_term_memory.messages[0][1] == "user"
    assert services.short_term_memory.messages[-1][1] == "assistant"


def test_turn_graph_builds_without_runtime_event_queue():
    graph = build_turn_graph()
    nodes = set(graph.get_graph().nodes)
    assert {"prepare", "classify", "finalize"} <= nodes
    assert {"shopping", "travel", "trip_gear", "composite", "react", "general_chat"} <= nodes


def test_node_lifecycle_comes_from_native_tasks_stream():
    """节点计时/成败来自 LangGraph tasks 流，不再依赖自研 run_policy_node。"""
    events = _run_graph(_FakeServices(intent="shopping", route="react"))
    logs = [event for event in events if event["event"] == "execution_log"]
    kinds = {log["data"]["kind"] for log in logs}
    assert "node_started" in kinds
    assert "node_succeeded" in kinds
    assert all(log["data"]["node"] for log in logs)


def test_graph_exposes_no_legacy_orchestrator_attribute():
    graph = TurnGraph(_FakeServices())
    assert not hasattr(graph, "legacy")
    assert graph.graph is not None


def _async_return(value):
    async def run(*args, **kwargs):
        return value

    return run
