"""trip_gear 图的端到端行为：约束、行程、装备、校验、修复。"""

import asyncio

from app.trip_gear.graph import TripGearGraph
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


def _run(message: str, history_text: str = "", **kwargs):
    graph = TripGearGraph(product_search=_search, **kwargs)
    return asyncio.run(graph.run(message, "user-1", history_text=history_text))


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

    assert state["status"] == "needs_info"
    assert "目的地" in state["response"]
    assert "天数" in state["response"]


def test_previous_turn_supplies_owned_items_and_destination():
    state = _run(
        "那装备清单呢",
        history_text="user: 周末去莫干山露营两天，我有帐篷和睡袋",
        planner=_Planner("到达营地"),
    )

    assert state["status"] == "ok"
    assert "目的地：莫干山（上文）" in state["response"]
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
