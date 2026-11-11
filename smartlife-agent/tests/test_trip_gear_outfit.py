"""追问边界不能把"女生想买一套衣服"丢掉；估算要把机票住宿算进预算。"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graphs.context import TurnContext
from app.graphs.subgraphs.trip_gear import build_trip_gear_graph
from app.trip_gear.models import DayPlan, Itinerary, ItineraryItem

ROWS = {
    "女装": [
        {"id": 1, "name": "碎花雪纺连衣裙", "price": 299, "stock": 100,
         "category": "服装", "subcategory": "女装", "brand": "ZARA", "lead_time_days": 3},
        {"id": 2, "name": "高腰A字半裙", "price": 229, "stock": 90,
         "category": "服装", "subcategory": "女装", "lead_time_days": 3},
        {"id": 3, "name": "针织修身上衣", "price": 159, "stock": 180,
         "category": "服装", "subcategory": "女装", "lead_time_days": 3},
    ],
    "泳衣": [{"id": 21, "name": "女士连体泳衣", "price": 169, "stock": 120,
              "category": "户外", "subcategory": "水上", "lead_time_days": 3}],
    "防晒霜": [{"id": 29, "name": "防晒霜SPF50+", "price": 89, "stock": 300,
                "category": "户外", "subcategory": "水上", "lead_time_days": 3}],
    "T恤": [{"id": 31, "name": "纯棉圆领T恤", "price": 89, "stock": 200,
             "category": "服装", "subcategory": "男装", "lead_time_days": 3}],
}


class _Planner:
    async def __call__(self, facts, constraints):
        days = []
        for day in range(1, int(facts.days or 7) + 1):
            activity = "海边沙滩散步" if day == 2 else "城市漫步"
            days.append(DayPlan(
                day=day,
                items=[ItineraryItem(time="10:00", activity=activity, duration_min=90, cost=50)],
            ))
        return Itinerary(destination=facts.destination, days=days), []


class _Services:
    def get_chat_history(self, thread_id):
        return []


def _search(query="", limit=30):
    return list(ROWS.get(query, []))


def test_outfit_and_estimates_survive_the_clarify_interrupts():
    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    ctx = TurnContext(services=_Services(), trace_id="outfit").with_extra(
        planner=_Planner(), product_search=_search
    )
    config = {"configurable": {"thread_id": "outfit"}}
    message = (
        "上海出发去广东，飞机，两个人情侣出行，5000块钱，去一周，"
        "要去很多城市，包括汕头，出一份完整计划，女生想买一套衣服带去海边"
    )

    first = asyncio.run(graph.ainvoke(
        {"user_message": message, "user_id": "u", "thread_id": "outfit"},
        config, context=ctx,
    ))
    assert first["__interrupt__"][0].value["type"] == "trip_gear_clarify"

    second = asyncio.run(graph.ainvoke(Command(resume="出发日期：明天"), config, context=ctx))
    question = second["__interrupt__"][0].value["questions"][0]
    assert question["type"] == "slider"
    assert question["budget"] == 5000
    # 机票 3680 + 住宿 1800：滑块旁边要能提示"行程额度不够覆盖这两块"
    assert question["estimate"] == 5480

    state = asyncio.run(graph.ainvoke(Command(resume="预算分配：90"), config, context=ctx))

    assert state["facts"]["purchase_items"] == ["女生想买一套衣服带去海边"]
    outfit = next(item for item in state["gear"] if item["name"] == "女装")
    assert outfit["user_requested"] is True
    assert "女生想买一套衣服" in outfit["reason"]

    # 预算分配 90/10：装备 500，但点名的女装必须留在清单里并配到商品
    assert state["products"].get("女装")

    itinerary = state["itinerary"]
    assert itinerary["intercity_cost"] == 3680
    assert itinerary["days"][-1]["lodging_cost"] == 0
    assert itinerary["days"][0]["lodging_cost"] == 300

    # 机票 + 住宿 + 地面 > 5000：必须报预算硬冲突，而不是"剩余 1176"
    assert any(
        issue.get("severity") == "critical" and str(issue.get("constraint", "")).startswith("预算")
        for issue in state["issues"]
    )


def test_outfit_tradeoff_is_disclosed_when_dress_is_skipped():
    """500 只能半裙时，要明说连衣裙被额度挤掉，不能只写"已按预算省略"。"""
    from app.trip_gear.models import GearItem, Issue
    from app.trip_gear.verify import _check_purchases, _check_specs
    from app.trip_gear.verify import verify
    # 直接走 verify：构造一个被省略的连衣裙 + 用户点名的女装
    from app.trip_gear.models import DayPlan, Itinerary, ItineraryItem, TripFacts

    facts = TripFacts(destination="广东", days=2, people=2, budget=5000, gear_cap=500)
    gear = [
        GearItem(name="女装", qty=1, status="need_buy", user_requested=True,
                reason="用户点名：女生想买一套衣服"),
        GearItem(name="连衣裙", qty=1, status="skipped",
                reason="用户点名：女生想买一套衣服；预算不足，已按额度省略"),
    ]
    itinerary = Itinerary(destination="广东", days=[
        DayPlan(day=1, items=[ItineraryItem(time="10:00", activity="海边", duration_min=60)]),
    ])
    issues = verify(facts, [], itinerary, gear, {}, today="2026-11-11")

    assert any("未选到连衣裙" in issue.constraint for issue in issues)


def test_mall_day_makes_late_delivery_self_pickup_not_infeasible():
    """行程里有商场，线上赶不上要判自提，不能判不可行。"""
    from datetime import date

    from app.trip_gear.models import (
        DayPlan, GearItem, Itinerary, ItineraryItem, TripFacts,
    )
    from app.trip_gear.verify import verify

    facts = TripFacts(
        destination="广东", days=2, people=2, budget=5000,
        gear_cap=500, trip_cap=4500, start_date="2026-11-12",
    )
    gear = [
        GearItem(name="女装", qty=1, status="need_buy", user_requested=True,
                reason="用户点名：女生想买一套衣服"),
        GearItem(name="防晒霜", qty=1, status="need_buy", reason="紫外线"),
    ]
    products = {
        "女装": {"name": "碎花雪纺连衣裙", "price": 299, "stock": 10, "lead_time_days": 3},
        "防晒霜": {"name": "防晒霜SPF50+", "price": 89, "stock": 300, "lead_time_days": 3},
    }
    itinerary = Itinerary(destination="广东", days=[
        DayPlan(day=1, items=[ItineraryItem(time="10:00", activity="海边", duration_min=60)]),
        DayPlan(day=2, items=[ItineraryItem(time="14:00", activity="商场逛街，挑衣服", duration_min=90)]),
    ])

    issues = verify(facts, [], itinerary, gear, products, today=date(2026, 11, 11))

    assert any("可到店自提" in issue.constraint for issue in issues)
    assert not any(
        issue.severity == "critical" and "到货赶不上" in issue.constraint
        for issue in issues
    )
