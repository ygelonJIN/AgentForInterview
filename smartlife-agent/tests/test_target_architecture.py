"""整改后的目标架构回归测试。"""

import asyncio
from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from app.graphs.plan_models import GeneralPlanStep, GeneralTaskPlan
from app.graphs.subgraphs.customer_service import build_customer_service_graph
from app.graphs.subgraphs.general_plan import build_general_plan_graph
from app.services.task_models import build_composite_plan, extract_shared_constraints
from app.graphs.context import TurnContext
from app.graphs.subgraphs.travel import build_travel_plan_graph
from app.tools.shopping_tools import GetOrderStatusInput, ShoppingToolProvider


def test_shared_constraints_are_extracted_and_reused_by_composite_plan():
    constraints = extract_shared_constraints("推荐800元以内的跑鞋，并规划杭州两天行程，预算3000元，2人出行")
    plan = build_composite_plan("推荐800元以内的跑鞋，并规划杭州两天行程，预算3000元，2人出行")

    assert constraints.budget in {800.0, 3000.0}
    assert constraints.people == 2
    assert constraints.days == 2
    assert constraints.destination == "杭州"
    assert plan.constraints == constraints
    assert [item.intent for item in plan.subtasks] == ["shopping", "travel"]
    assert {"sql", "weather", "route"} <= set(plan.evidence_sources)


def test_order_tool_schema_has_no_model_controlled_user_id():
    fields = set(GetOrderStatusInput.model_fields)
    provider = ShoppingToolProvider(actor_id="user-001")
    order_tool = next(tool for tool in provider.get_tools() if tool.name == "get_order_status")

    assert "user_id" not in fields
    assert "order_id" in fields
    rows = order_tool.invoke({})
    assert all(row["user_id"] == "user-001" for row in rows)


def test_customer_service_graph_reads_only_actor_orders():
    calls = []

    class Repository:
        def get_order_status(self, user_id, order_id):
            calls.append((user_id, order_id))
            return [{"id": 1, "product_name": "跑鞋", "status": "completed", "total_price": 399, "user_id": user_id}]

    llm = RunnableLambda(lambda _: SimpleNamespace(content="你的订单已完成。"))

    class Services:
        order_repository = Repository()

        def __init__(self):
            self.llm = llm

    ctx = TurnContext(services=Services(), trace_id="cs")
    result = asyncio.run(build_customer_service_graph().ainvoke(
        {"user_message": "我的订单怎么样", "user_id": "user-001", "status": "started"},
        context=ctx,
    ))

    assert calls == [("user-001", None)]
    assert "订单已完成" in result["response"]


def test_general_plan_graph_executes_tools_and_synthesizes():
    calls = []

    async def planner(request, context, previous_plan=None, feedback=None):
        return GeneralTaskPlan(
            summary="任务计划",
            steps=[
                GeneralPlanStep(title="查时间", instruction="获取当前时间", tool_name="get_current_time", arguments={}),
                GeneralPlanStep(title="整理", instruction="整理结果"),
            ],
        )

    async def executor(step):
        calls.append(step.tool_name)
        return {"status": "completed", "result": "2026-10-08" if step.tool_name else "done"}

    llm = RunnableLambda(lambda _: SimpleNamespace(content="最终任务结果"))

    class Services:
        def __init__(self):
            self.llm = llm

        def tools_for(self, actor_id, agent_id="general"):
            return []

    ctx = TurnContext(services=Services(), trace_id="plan").with_extra(
        planner=planner, executor=executor
    )
    result = asyncio.run(build_general_plan_graph().ainvoke(
        {"user_message": "制定今天的任务安排", "user_id": "user-001", "status": "started"},
        context=ctx,
    ))

    assert calls == ["get_current_time", None]
    assert result["response"] == "最终任务结果"


def test_travel_plan_uses_tool_evidence_before_first_plan():
    planner_contexts = []

    async def planner(request, context, previous_plan=None, feedback=None, on_token=None):
        planner_contexts.append(context)
        return "第1天：A"

    async def reflector(request, plan):
        return {"is_satisfactory": True}

    async def executor(step, state):
        return {"status": "completed", "result": f"真实证据:{step['id']}"}

    class _TravelService:
        def _build_plan_context(self, *_args, **_kwargs):
            return "上下文"

        @staticmethod
        def _remove_revision_notes(plan):
            return plan

    class _Services:
        travel_service = _TravelService()
        llm = None

        def get_chat_history(self, thread_id):
            return []

    ctx = TurnContext(services=_Services(), trace_id="travel:evidence").with_extra(
        planner=planner, reflector=reflector, executor=executor, max_revisions=0,
    )
    asyncio.run(build_travel_plan_graph().ainvoke(
        {"user_message": "上海1天预算500元", "user_id": "user-1", "thread_id": "travel:evidence"},
        context=ctx,
    ))

    assert len(planner_contexts) == 1
    assert "真实工具证据" in planner_contexts[0]
    assert "真实证据:evidence-weather" in planner_contexts[0]


def test_composite_branch_failure_is_preserved_for_partial_merge():
    """单分支失败时，另一分支结果仍进入最终正文。"""
    from app.graphs.orchestrator import TurnGraph

    class Services:
        def __init__(self):
            from tests.test_turn_graph import _Memory, _fake_llm

            self.short_term_memory = _Memory()
            self.llm = _fake_llm()
            self.travel_service = SimpleNamespace(
                plan_trip_streaming=_raise("travel unavailable"),
                chat_streaming=_raise("travel unavailable"),
            )
            self.shopping_fails = True

        async def classify(self, user_message, thread_id, queue):
            from app.classifier import ClassificationResult

            return ClassificationResult(
                route="plan_and_execute",
                intent="travel",
                intents=["shopping", "travel"],
                sub_intent="plan_trip",
                retrieval="mixed",
                confidence=1.0,
                reason="test",
            )

        def get_chat_history(self, thread_id):
            return []

        async def begin_turn(self, user_message, thread_id):
            self.short_term_memory.add_message(thread_id, "user", user_message)

        async def finish_turn(self, thread_id, response, classification, queue):
            self.short_term_memory.add_message(thread_id, "assistant", response)
            await queue.emit_done({"response": response})

        async def plan_shopping_retrieval(self, *_args, **_kwargs):
            raise RuntimeError("shopping unavailable")

        async def retrieve_shopping_context(self, *_args, **_kwargs):
            raise RuntimeError("shopping unavailable")

        async def generate_shopping_response(self, *_args, **_kwargs):
            raise RuntimeError("shopping unavailable")

        def validate_shopping_response(self, *_args, **_kwargs):
            return None

        async def synthesize_composite_response(self, message, shopping, travel, constraints, queue):
            assert shopping["status"] == "failed"
            return "购物未完成；旅行计划：" + str(travel.get("response") or travel.get("error"))

    async def run():
        graph = TurnGraph(Services())
        return [
            line async for line in graph.process_streaming(
                "推荐跑鞋并规划杭州两日游", "user-1", "browser-1"
            )
        ]

    lines = asyncio.run(run())
    assert any("购物未完成" in line for line in lines)


def _raise(message):
    async def run(*_args, **_kwargs):
        raise RuntimeError(message)

    return run
