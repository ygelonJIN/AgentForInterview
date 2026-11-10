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
    assert {"shopping", "travel", "trip_gear", "general", "customer_service"} <= nodes
    assert not {"composite", "general_chat", "general_plan", "react"} & nodes


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


def test_resume_records_the_clarify_answer_as_a_user_message():
    """补答也是用户消息：resume 从 interrupt 处继续，prepare 不重跑。

    不在进图之前补记一笔，库里就缺这句，内存顺序和库顺序会错位，
    旧消息迁移随后每次重跑都会把那一轮再写一遍。
    """
    from app.session import build_thread_id

    services = _FakeServices()
    graph = TurnGraph.__new__(TurnGraph)
    graph.services = services
    graph.checkpointer = None

    async def _stub_stream(graph_input, config, context, trace_id):
        yield 'data: {"event": "done"}\n\n'

    graph._stream_graph = _stub_stream

    async def run():
        return [line async for line in graph.resume_streaming("人数：1 人", "u1", "s1")]

    lines = asyncio.run(run())

    assert len(lines) == 1
    assert services.short_term_memory.messages == [
        (build_thread_id("u1", "general", "s1"), "user", "人数：1 人"),
    ]
