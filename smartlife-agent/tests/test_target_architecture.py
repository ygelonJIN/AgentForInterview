"""整改后的目标架构回归测试。"""

import asyncio
from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from app.agents.customer_service_graph import CustomerServiceGraph
from app.agents.general_plan_graph import GeneralPlanGraph, GeneralPlanStep, GeneralTaskPlan
from app.agents.task_models import build_composite_plan, extract_shared_constraints
from app.agents.travel_graph import TravelPlanGraph
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
    graph = CustomerServiceGraph(Repository(), llm)
    response = asyncio.run(graph.run_streaming("我的订单怎么样", "user-001"))

    assert calls == [("user-001", None)]
    assert "订单已完成" in response


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
    graph = GeneralPlanGraph(llm, planner=planner, executor=executor)
    response = asyncio.run(graph.run_streaming("制定今天的任务安排"))

    assert calls == ["get_current_time", None]
    assert response == "最终任务结果"


def test_travel_plan_uses_tool_evidence_before_first_plan():
    planner_contexts = []

    async def planner(request, context, previous_plan=None, feedback=None, on_token=None):
        planner_contexts.append(context)
        return "第1天：A"

    async def reflector(request, plan):
        return {"is_satisfactory": True}

    async def executor(step, state):
        return {"status": "completed", "result": f"真实证据:{step['id']}"}

    graph = TravelPlanGraph(planner=planner, reflector=reflector, executor=executor)
    asyncio.run(graph.run_streaming("上海1天预算500元", "user-1", "", thread_id="travel:evidence"))

    assert len(planner_contexts) == 1
    assert "真实工具证据" in planner_contexts[0]
    assert "真实证据:evidence-weather" in planner_contexts[0]


def test_composite_branch_failure_is_preserved_for_partial_merge():
    from app.agents.langgraph_orchestrator import LangGraphOrchestrator

    class Legacy:
        async def begin_turn(self, *args):
            return None

        async def classify_turn(self, *args):
            return SimpleNamespace(
                intent="travel",
                route="plan_and_execute",
                intents=["shopping", "travel"],
                model_dump=lambda: {
                    "intent": "travel",
                    "intents": ["shopping", "travel"],
                    "route": "plan_and_execute",
                },
            )

        async def run_shopping_turn(self, *args, **kwargs):
            raise RuntimeError("shopping unavailable")

        async def run_travel_turn(self, *args, **kwargs):
            return "杭州两日游"

        async def synthesize_composite_response(self, message, shopping, travel, constraints, queue):
            assert shopping["status"] == "failed"
            assert travel["status"] == "ok"
            return "购物未完成；旅行计划：杭州两日游"

        def extract_candidate_memories_result(self, *args, **kwargs):
            return SimpleNamespace(as_dict=lambda: {
                "candidates": [], "diagnostic": "", "status": "no_content",
                "has_error": False, "should_display": False,
            })

        async def finish_turn(self, thread_id, response, classification, queue):
            await queue.emit("done", {"response": response}, step="done")

    async def run():
        wrapper = LangGraphOrchestrator(Legacy())
        lines = [
            line async for line in wrapper.process_streaming(
                "推荐跑鞋并规划杭州两日游", "user-1", "browser-1"
            )
        ]
        return lines

    lines = asyncio.run(run())
    assert any("购物未完成" in line for line in lines)
