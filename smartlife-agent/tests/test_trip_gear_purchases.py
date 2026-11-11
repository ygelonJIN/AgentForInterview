"""历史订单进"已有装备"：买过的东西不再重复列进缺口。"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graphs.context import TurnContext
from app.graphs.subgraphs.trip_gear import build_trip_gear_graph
from app.trip_gear.owned import owned_from_purchases


class _Services:
    def get_chat_history(self, thread_id):
        return []


class _Planner:
    async def __call__(self, facts, constraints):
        from app.trip_gear.itinerary import fallback_itinerary

        return fallback_itinerary(facts), []


def _fmt(item):
    return {"price": 100, "brand": "测试", "stock": 5}


def test_purchases_map_to_owned_items():
    owned = owned_from_purchases([
        {"name": "双人防水帐篷", "quantity": 1},
        {"name": "双人防水帐篷", "quantity": 1},
        {"name": "", "quantity": 1},
    ])

    assert [item.name for item in owned] == ["双人防水帐篷"]
    assert owned[0].source == "历史订单"


def test_purchase_history_marks_gear_as_owned():
    extra = {
        "planner": _Planner(),
        "product_search": lambda query="", limit=5: [_fmt(query)],
        "purchase_history": lambda _uid: [{"name": "双人防水帐篷", "quantity": 1}],
    }
    ctx = TurnContext(services=_Services(), trace_id="purchases").with_extra(**extra)
    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "purchases"}}
    payload = {
        "user_message": "周末两个人去莫干山露营，一个人，明天出发，预算3000",
        "user_id": "user_001",
        "thread_id": "purchases",
    }

    state = asyncio.run(graph.ainvoke(payload, config, context=ctx))
    if state.get("__interrupt__"):
        state = asyncio.run(graph.ainvoke(Command(resume="50/50"), config, context=ctx))

    tents = [item for item in state["gear"] if "帐篷" in item["name"]]
    assert tents, "露营行程应当推导出帐篷"
    assert tents[0]["status"] == "owned"
    assert any(item.get("source") == "历史订单" for item in state["owned"])
