"""预算分配：trip_gear 在排行程之前先把预算拆成行程额度与装备额度。"""

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graphs.context import TurnContext
from app.graphs.subgraphs.trip_gear import build_trip_gear_graph
from app.trip_gear import split


class _Services:
    def get_chat_history(self, thread_id):
        return []


class _Planner:
    async def __call__(self, facts, constraints):
        from app.trip_gear.itinerary import fallback_itinerary

        return fallback_itinerary(facts), []


def _graph_and_ctx(with_gear=True):
    ctx = TurnContext(services=_Services(), trace_id="split").with_extra(planner=_Planner())
    return build_trip_gear_graph(checkpointer=InMemorySaver(), with_gear=with_gear), ctx


def _start(message, thread="split-1", with_gear=True):
    graph, ctx = _graph_and_ctx(with_gear=with_gear)
    payload = {"user_message": message, "user_id": "u", "thread_id": thread}
    config = {"configurable": {"thread_id": thread}}
    return graph, ctx, config, asyncio.run(graph.ainvoke(payload, config, context=ctx))


def test_parse_accepts_presets_percent_and_amount():
    assert split.parse("预算分配：80/20", 5000)["gear_cap"] == 1000
    assert split.parse("50/50", 5000)["gear_cap"] == 2500
    assert split.parse("装备 30%", 5000)["gear_cap"] == 1500
    assert split.parse("装备预算 1000", 5000)["gear_cap"] == 1000
    assert split.parse("行程 4000", 5000)["gear_cap"] == 1000
    assert split.parse("随便", 5000) is None


def test_caps_never_exceed_budget():
    assert split.caps(5000, 8000) == (0.0, 5000.0)
    assert split.caps(5000, -100) == (5000.0, 0.0)


def test_budget_split_asks_before_planning():
    _graph, _ctx, _config, first = _start("周末两个人去莫干山露营，预算3000")

    payload = first["__interrupt__"][0].value
    assert payload["type"] == "trip_gear_budget_split"
    question = payload["questions"][0]
    assert question["type"] == "slider"
    assert (question["min"], question["max"], question["step"]) == (0, 100, 10)


def test_resume_applies_chosen_ratio():
    graph, ctx, config, first = _start("周末两个人去莫干山露营，预算3000")
    assert first["__interrupt__"]

    state = asyncio.run(graph.ainvoke(Command(resume="20"), config, context=ctx))

    assert state["budget_split"]["trip_cap"] == 600.0
    assert state["budget_split"]["gear_cap"] == 2400.0
    assert state["budget_split"]["source"] == "用户选择"
    assert any(item.get("key") == "budget_split" for item in state["constraints"])


def test_user_stated_gear_budget_skips_the_question():
    _graph, _ctx, _config, state = _start("周末两个人去莫干山露营，预算3000，装备预算800")

    assert not state.get("__interrupt__")
    assert state["budget_split"]["gear_cap"] == 800.0


def test_travel_does_not_ask_about_splitting():
    _graph, _ctx, _config, state = _start("去杭州玩两天，一个人，明天出发，预算3000", with_gear=False)

    assert not state.get("__interrupt__")
    assert not state.get("budget_split")


@pytest.mark.parametrize("answer,gear", [("80", 600.0), ("50", 1500.0)])
def test_ratio_is_applied_to_the_budget(answer, gear):
    graph, ctx, config, _first = _start("周末两个人去莫干山露营，预算3000", thread=f"split-{answer}")
    state = asyncio.run(graph.ainvoke(Command(resume=answer), config, context=ctx))
    assert state["budget_split"]["gear_cap"] == gear


def test_equipment_spend_never_exceeds_the_gear_cap():
    """额度是用户选的，装备总花费就不能超过它——模型挑款也一样。"""
    from app.trip_gear.verify import gear_cost

    graph, ctx, config, _first = _start("周末两个人去莫干山露营，预算1000", thread="split-cap")
    state = asyncio.run(graph.ainvoke(Command(resume="20"), config, context=ctx))

    from app.trip_gear.models import GearItem

    items = [GearItem.model_validate(item) for item in state["gear"]]
    spent, _unknown = gear_cost(items, state.get("products") or {})
    assert spent <= state["budget_split"]["gear_cap"] + 1e-6


def test_shopper_reserves_money_so_later_items_still_fit():
    """前面那件按"评分最高"会把钱花光时，退到够用的便宜款，保证后面也买得起。"""
    from app.trip_gear.models import GearItem
    from app.trip_gear.shopping import GearShopper

    rows = {
        "帐篷": [
            {"id": 1, "name": "豪华帐篷", "price": 599, "stock": 5, "rating": 5.0},
            {"id": 2, "name": "基础帐篷", "price": 299, "stock": 5, "rating": 3.6},
        ],
        "睡袋": [
            {"id": 3, "name": "四季睡袋", "price": 299, "stock": 5, "rating": 4.5},
        ],
    }

    def search(query="", limit=30):
        return list(rows.get(query, []))[:limit]

    gear = [
        GearItem(name="帐篷", qty=1, status="need_buy"),
        GearItem(name="睡袋", qty=1, status="need_buy"),
    ]
    products: dict = {}

    from app.trip_gear.models import TripFacts

    facts = TripFacts(destination="莫干山", days=2, people=2, lodging="camp", budget=700)
    GearShopper(search, facts=facts).fill(gear, products, remaining_budget=700)

    assert set(products) == {"帐篷", "睡袋"}, "两件都该买到，只是帐篷退到便宜款"
    assert products["帐篷"]["price"] == 299
    assert sum(item["price"] for item in products.values()) <= 700


def test_slider_at_100_means_travel_only():
    """滑条拉到 100% 行程 = 不要装备：装备段整段跳过，输出等价 travel。"""
    graph, ctx, config, _first = _start("周末两个人去莫干山露营，预算3000", thread="split-100")
    state = asyncio.run(graph.ainvoke(Command(resume="100"), config, context=ctx))

    assert state["split_mode"] == "travel"
    assert state["with_gear"] is False
    assert not state.get("gear")
    assert state["itinerary"]["days"], "行程照常生成"


def test_slider_at_0_means_gear_only():
    """滑条拉到 0% 行程 = 只买装备：不排行程，只出装备清单。"""
    graph, ctx, config, _first = _start("周末两个人去莫干山露营，预算3000", thread="split-0")
    state = asyncio.run(graph.ainvoke(Command(resume="0"), config, context=ctx))

    assert state["split_mode"] == "gear_only"
    assert not state.get("itinerary"), "不该生成逐日行程"
    assert state["gear"], "装备清单要照常推出来"
    assert state["budget_split"]["trip_cap"] == 0.0
    assert state["budget_split"]["gear_cap"] == 3000.0


def test_panel_total_includes_lodging_like_verify():
    """面板合计必须和 verify 同口径（条目 + 住宿），否则一个说没超、一个说超。"""
    from app.ui.theme import plan_panels_html

    payload = {
        "facts": {"budget": 5000, "destination": "广东", "people": 2, "budget_scope": "all_in"},
        "itinerary": {"destination": "广东", "days": [
            {"day": 1, "items": [{"time": "09:00", "activity": "外滩", "cost": 1480}],
             "lodging_cost": 600},
            {"day": 2, "items": [{"time": "08:00", "activity": "小公园", "cost": 1500}],
             "lodging_cost": 600},
        ]},
        "gear": [], "products": {}, "issues": [], "constraints": [],
    }

    html = plan_panels_html(payload)

    assert "行程合计 ¥4180" in html, "2980 条目 + 1200 住宿"


def test_card_does_not_ask_what_the_user_already_said():
    """已经说过的预算/人数不能再问一遍——之前是因为预算没解析出来，卡片才补问。"""
    from app.services.clarify_questions import build_questions
    from app.trip_gear.constraints import derive_constraints

    facts, _ = derive_constraints(
        "上海，去广东，情侣，一周，5000块，要去很多城市，包含汕头，"
        "做一个规划，我有一个pocket3，酒店找300左右一晚"
    )

    questions = build_questions(["出发日期"], facts)

    assert [q["key"] for q in questions] == ["start_date"]
