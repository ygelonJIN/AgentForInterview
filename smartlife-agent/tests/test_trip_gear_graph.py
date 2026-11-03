"""trip_gear 图的端到端行为：约束、行程、装备、校验、修复。"""

import asyncio

from app.graphs.context import TurnContext
from app.graphs.subgraphs.trip_gear import build_trip_gear_graph
from app.trip_gear.models import DayPlan, GearItem, Itinerary, ItineraryItem

PRICES = {
    "防晒霜": 89,
    "帽子": 59,
    "墨镜": 300,
    "泳衣": 169,
    "防水袋": 49,
    "速干衣": 50,
    "帐篷": 599,
    "睡袋": 299,
    "防潮垫": 89,
    "头灯": 59,
    "防水鞋": 459,
    "保暖层": 599,
    "T恤": 89,
}


class _Planner:
    def __init__(self, *activities: str):
        self.activities = activities or ("到达营地",)

    async def __call__(self, facts, constraints):
        return Itinerary(
            destination=facts.destination,
            days=[
                DayPlan(
                    day=day,
                    items=[
                        ItineraryItem(
                            time="10:00", activity=activity, duration_min=90, cost=50
                        )
                        for activity in self.activities
                    ],
                )
                for day in range(1, int(facts.days or 2) + 1)
            ],
        )


def _search(query: str = "", limit: int = 5):
    return [
        {
            "name": f"{query}商品",
            "brand": "测试",
            "price": PRICES.get(query, 100),
            "stock": 10,
        }
    ]


class _Services:
    """装备子图只需要历史；其余依赖通过 context.extra 注入。"""

    def __init__(self, chat_history=None):
        self._history = list(chat_history or [])

    def get_chat_history(self, thread_id):
        return self._history


def _run(message: str, history_text: str = "", chat_history=None, **kwargs):
    extra = {"product_search": _search}
    extra.update(kwargs)
    services = _Services(chat_history)
    ctx = TurnContext(services=services, trace_id="trip-gear").with_extra(**extra)
    payload = {"user_message": message, "user_id": "user-1", "thread_id": "trip-gear"}
    if history_text:
        payload["history_text"] = history_text
    return asyncio.run(build_trip_gear_graph().ainvoke(payload, context=ctx))


def test_full_chain_renders_constraints_itinerary_and_gear():
    state = _run(
        "周末两个人去莫干山露营，预算1500，我有帐篷和睡袋",
        planner=_Planner("到达营地", "湖边徒步"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 8,
            "temperature_max": 18,
            "condition": "多云",
            "precipitation_probability": 60,
        },
    )

    assert state["status"] == "ok"
    response = state["response"]
    assert "## 约束清单" in response
    assert "睡袋温标 ≤ 3℃" in response
    assert "## 行程" in response
    assert "## 装备清单" in response
    assert "已有" in response
    assert "睡袋 ×1" in response  # 2 人 - 已有 1 件


def test_owned_items_from_memory_are_used():
    state = _run(
        "周末两个人去莫干山露营，预算3000",
        planner=_Planner("到达营地"),
        memory_loader=lambda _user_id, _query: [
            {
                "content": "用户上周买了帐篷和睡袋",
                "metadata": {"memory_id": "abcd12345678"},
            }
        ],
    )

    response = state["response"]
    assert "已有（记忆:abcd1234）" in response
    assert "帐篷 ×1" in response


def test_over_budget_repair_skips_optional_gear():
    state = _run(
        "周末去三亚玩两天，预算500",
        planner=_Planner("海边游泳"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 30,
            "temperature_max": 35,
            "condition": "晴",
            "precipitation_probability": 5,
        },
    )

    assert state["revision"] == 2
    assert "已按预算省略" in state["response"]
    assert "墨镜" in state["response"]
    assert not any(
        issue["constraint"].startswith("预算") for issue in state["issues"]
    )


def test_missing_core_fields_are_asked_back():
    state = _run("帮我列个装备清单")

    interrupts = state.get("__interrupt__") or []
    assert interrupts, "缺少核心信息时应中断并追问"
    payload = interrupts[0].value
    assert payload["missing"] == ["目的地", "天数"]
    assert "目的地" in payload["question"]
    assert "天数" in payload["question"]


def test_clarify_resume_fills_missing_fields_and_continues():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-resume"}}
    ctx = TurnContext(
        services=_Services(None),
        trace_id="trip-gear",
    ).with_extra(product_search=_search, planner=_Planner("到达杭州"))

    first = asyncio.run(
        graph.ainvoke(
            {"user_message": "帮我列个装备清单", "user_id": "user-1", "thread_id": "trip-gear-resume"},
            cfg,
            context=ctx,
        )
    )
    assert first.get("__interrupt__"), "第一轮应停下等补充"

    second = asyncio.run(
        graph.ainvoke(Command(resume="去杭州玩两天"), cfg, context=ctx)
    )
    assert second["status"] == "ok"
    assert "## 行程" in second["response"]
    assert "目的地：杭州" in second["response"]
    assert "天数：2 天" in second["response"]


def test_previous_turn_supplies_owned_items_and_destination():
    state = _run(
        "那装备清单呢",
        history_text="user: 周末去莫干山露营两天，我有帐篷和睡袋",
        planner=_Planner("到达营地"),
    )

    assert state["status"] == "ok"
    assert "目的地：莫干山" in state["response"]
    assert "来源：上文" in state["response"]
    assert "已有（上文）" in state["response"]


class _PlannerWithSuggestions(_Planner):
    async def __call__(self, facts, constraints):
        itinerary = await super().__call__(facts, constraints)
        return itinerary, [
            GearItem(name="裙子", qty=2, reason="海边度假"),
            GearItem(name="防晒霜", qty=1, reason="户外日晒"),
        ]


def test_model_suggestions_extend_rule_gear():
    state = _run(
        "周末去三亚玩两天，预算2000",
        planner=_PlannerWithSuggestions("海边散步"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 30,
            "temperature_max": 35,
            "condition": "晴",
            "precipitation_probability": 5,
        },
    )

    response = state["response"]
    assert "裙子 ×2" in response
    assert "海边度假" in response
    sunscreen_lines = [
        line for line in response.splitlines() if line.startswith("- 防晒霜")
    ]
    assert len(sunscreen_lines) == 1  # 与规则同名，合并而不是重复
