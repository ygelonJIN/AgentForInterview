"""整改后的目标架构回归测试。"""

import asyncio
from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from app.graphs.subgraphs.customer_service import build_customer_service_graph
from app.services.task_models import build_composite_plan, extract_shared_constraints
from app.graphs.context import TurnContext
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


def _raise(message):
    async def run(*_args, **_kwargs):
        raise RuntimeError(message)

    return run


def test_shared_constraints_parse_wan_and_week():
    """“预算1万块 / 玩一周”必须解析成 10000 元 / 7 天。"""
    from app.services.task_models import extract_shared_constraints, parse_budget, parse_days

    message = "我要去上海旅游，玩一周，预算1万块。我有一条西装裤子。"
    constraints = extract_shared_constraints(message)
    assert constraints.budget == 10000
    assert constraints.days == 7
    assert constraints.destination == "上海"

    assert parse_budget("预算一万块") == 10000
    assert parse_budget("预算 1.5万") == 15000
    assert parse_budget("1万5") == 15000
    assert parse_budget("3千") == 3000
    assert parse_days("两周") == 14
    assert parse_days("半个月") == 15


