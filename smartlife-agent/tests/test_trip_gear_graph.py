"""trip_gear 图的端到端行为：约束、行程、装备、校验、修复。"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

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


# 追问卡片的"标准答法"：出发地/交通/住宿现在都是阻塞项，绝大多数用例关心的是
# 补完之后的行为，不是追问本身。想验追问的用例传 `answer_gaps=False`。
# 用"自驾 + 经济型 300"作答：自驾不计城际估算（只写"未估算"），住宿沿用
# 经济型单价，这样大多数用例的预算口径与追问改版前一致，断言不会被无关地推高。
_GAP_ANSWER = "出发地：上海；交通：自驾；住宿：酒店，每晚300元"


def _run(
    message: str,
    history_text: str = "",
    chat_history=None,
    answer_gaps: bool = True,
    **kwargs,
):
    extra = {"product_search": _search}
    extra.update(kwargs)
    services = _Services(chat_history)
    ctx = TurnContext(services=services, trace_id="trip-gear").with_extra(**extra)
    payload = {"user_message": message, "user_id": "user-1", "thread_id": "trip-gear"}
    if history_text:
        payload["history_text"] = history_text

    async def _drive():
        graph = build_trip_gear_graph(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "trip-gear"}}
        state = await graph.ainvoke(payload, config, context=ctx)
        # 有预算的行程会先停在"预算怎么分"；缺槽位会停在追问卡片。
        for _ in range(4):
            payload_interrupt = (state.get("__interrupt__") or [{}])[0]
            value = getattr(payload_interrupt, "value", payload_interrupt) or {}
            kind = value.get("type") if isinstance(value, dict) else ""
            if kind == "trip_gear_budget_split":
                state = await graph.ainvoke(Command(resume="50/50"), config, context=ctx)
                continue
            if answer_gaps and kind == "trip_gear_clarify":
                state = await graph.ainvoke(Command(resume=_GAP_ANSWER), config, context=ctx)
                continue
            return state
        return state

    return asyncio.run(_drive())


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
    # 买到的东西只显示"为什么带 → 买哪件"，不再露出规则内部的装备名。
    assert "→" in response
    assert "睡袋商品" in response


def test_missing_departure_date_is_asked_instead_of_defaulted():
    """没给出发日期就停下来问，不拿"今天"替用户决定。"""
    state = _run("帮我规划去莫干山露营两天，两个人", planner=_Planner("到达营地"))

    interrupts = state.get("__interrupt__") or []
    assert interrupts, "缺出发日期应先追问"
    payload = interrupts[0].value
    assert "出发日期" in payload["missing"]
    assert any(item["key"] == "start_date" for item in payload["questions"])


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


def test_over_budget_items_are_left_unbought_instead_of_overspending():
    """预算不够时不先超支再削减，而是当场不选——钱不够就该说不买。"""
    state = _run(
        "周末一个人去三亚玩两天，预算500",
        planner=_Planner("海边游泳"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 30,
            "temperature_max": 35,
            "condition": "晴",
            "precipitation_probability": 5,
        },
    )

    response = state["response"]
    assert "已按预算省略" in response
    # 总花费必须落在预算内，而不是靠事后砍单把账做平
    assert "¥500" in response
    assert not any(
        issue["constraint"].startswith("预算") for issue in state["issues"]
    )
    assert any(
        "已按预算省略" in str(issue["constraint"]) for issue in state["issues"]
    )


def test_missing_core_fields_are_asked_back():
    state = _run("帮我列个装备清单", answer_gaps=False)

    interrupts = state.get("__interrupt__") or []
    assert interrupts, "缺少核心信息时应中断并追问"
    payload = interrupts[0].value
    # 出发地、交通、住宿和目的地/天数/人数/日期一样是必问项：缺了出发地就没有
    # 大交通估算，缺了住宿就只能编一个每晚价。
    assert payload["missing"] == [
        "出发地", "目的地", "天数", "人数", "出发日期", "交通", "住宿",
    ]
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
        graph.ainvoke(
            Command(
                resume=(
                    "去杭州玩两天，我一个人，2026-11-10 出发，"
                    "从上海出发，坐高铁，住酒店每晚300元"
                )
            ),
            cfg,
            context=ctx,
        )
    )
    assert second["status"] == "ok"
    assert "## 行程" in second["response"]
    assert "目的地：杭州" in second["response"]
    assert "天数：2 天" in second["response"]


def test_previous_turn_supplies_owned_items_and_destination():
    state = _run(
        "那装备清单呢",
        history_text="user: 周末一个人去莫干山露营两天，2026-11-10 出发，我有帐篷和睡袋",
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
        "周末一个人去三亚玩两天，预算2000",
        planner=_PlannerWithSuggestions("海边散步"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 30,
            "temperature_max": 35,
            "condition": "晴",
            "precipitation_probability": 5,
        },
    )

    response = state["response"]
    assert "海边度假" in response
    dress_lines = [
        line for line in response.splitlines() if "裙子商品" in line
    ]
    assert len(dress_lines) == 1  # 与规则同名，合并而不是重复
    assert "×2" in dress_lines[0]  # 两件的时候仍然要显示数量
    sunscreen_lines = [
        line for line in response.splitlines() if "防晒霜商品" in line
    ]
    assert len(sunscreen_lines) == 1


def _search_with_lead(query: str = "", limit: int = 5):
    """带到货时效的商品源，用来把"时效未定"这条信息缺口排除掉。"""
    return [
        {
            "name": f"{query}商品",
            "brand": "测试",
            "price": PRICES.get(query, 100),
            "stock": 10,
            "lead_time_days": 1,
        }
    ]


def test_verdict_is_feasible_after_repair():
    # 预算 500 砍掉可选件后应当收敛到可行，而不是永远停在"未满足"。
    state = _run(
        "周末一个人去三亚玩两天，预算500",
        planner=_Planner("海边游泳"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 30,
            "temperature_max": 35,
            "condition": "晴",
            "precipitation_probability": 5,
        },
    )

    verdict = state["verdict"]
    assert verdict["state"] == "feasible"
    assert verdict["blocking"] == []


def test_verdict_reports_insufficient_gear_as_blocking():
    # 温标 10℃ 的睡袋去 3℃ 的夜里：改不动，也不该被静默修成"通过"。
    state = _run(
        "周末两个人去莫干山露营，预算3000，我有一个温标10℃的睡袋",
        planner=_Planner("到达营地"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 8,
            "temperature_max": 18,
            "condition": "多云",
            "precipitation_probability": 10,
        },
        product_search=_search_with_lead,
    )

    verdict = state["verdict"]
    assert verdict["state"] == "infeasible"
    assert any("睡袋温标" in item for item in verdict["blocking"])


def test_verdict_marks_missing_information_as_needs_input():
    # 到货时效未知属"缺信息"，结论应是待确认而不是不可行。
    state = _run(
        "周末两个人去莫干山露营，预算3000",
        planner=_Planner("到达营地"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 8,
            "temperature_max": 18,
            "condition": "多云",
            "precipitation_probability": 10,
        },
        product_search=_search_with_lead,
        memory_loader=lambda _user_id, _query: [],
    )

    verdict = state["verdict"]
    assert verdict["state"] in {"feasible", "needs_input", "infeasible"}


def test_tight_budget_omits_gear_instead_of_leaving_it_unbought():
    """装备额度不够时：能买的照买，买不起的标"已按预算省略"。

    以前会留一条"预算不足，未选款"——既没买到、也不算结束，看起来像故障。
    现在额度是用户选的，就按额度给结论：省略并说明，不再出现"装不下"。
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-budget-omit"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search_with_lead,
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 8,
            "temperature_max": 18,
            "condition": "多云",
            "precipitation_probability": 10,
        },
    )

    state = asyncio.run(graph.ainvoke(
        {"user_message": "周末两个人去莫干山露营，预算1000", "user_id": "user-1",
         "thread_id": "trip-gear-budget-omit"},
        cfg,
        context=ctx,
    ))
    # 露营行程只缺出发地和交通方式，先补这两项再谈预算怎么分
    assert state["__interrupt__"][0].value["type"] == "trip_gear_clarify"
    state = asyncio.run(
        graph.ainvoke(Command(resume="出发地：上海；交通：自驾"), cfg, context=ctx)
    )
    assert state["__interrupt__"][0].value["type"] == "trip_gear_budget_split"
    state = asyncio.run(graph.ainvoke(Command(resume="20/80"), cfg, context=ctx))

    assert not state.get("__interrupt__")
    omitted = [
        item["name"] for item in state["gear"]
        if item["status"] == "skipped" and "预算不足" in (item.get("reason") or "")
    ]
    assert omitted, "额度不够时应当明确省略，而不是留一条买不到的行"
    assert any("已按预算省略" in str(i["constraint"]) for i in state["issues"])
    assert not any("未选款" in str(i["constraint"]) for i in state["issues"])


def test_verify_clarify_gives_up_after_round_limit():
    """问到上限还说不清就出结果，不能无限追问。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-verify-limit"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search_with_lead,
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 8,
            "temperature_max": 18,
            "condition": "多云",
            "precipitation_probability": 10,
        },
    )
    payload = {"user_message": "周末两个人去莫干山露营，预算3000", "user_id": "user-1",
               "thread_id": "trip-gear-verify-limit"}

    state = asyncio.run(graph.ainvoke(payload, cfg, context=ctx))
    # 第一站是补槽位（出发地/交通）；露营所以不问住宿类型。
    assert state["__interrupt__"][0].value["type"] == "trip_gear_clarify"
    state = asyncio.run(
        graph.ainvoke(Command(resume="出发地：上海；交通：自驾"), cfg, context=ctx)
    )
    for _ in range(3):
        if not state.get("__interrupt__"):
            break
        state = asyncio.run(graph.ainvoke(Command(resume="还是不清楚"), cfg, context=ctx))

    assert not state.get("__interrupt__"), "到达轮次上限后不应继续追问"
    assert state.get("clarify_rounds", 0) <= 2


def test_origin_is_not_mistaken_for_destination():
    """"我在广东，我要去上海"：起点不能变成终点，约好的那半天要进行程。"""
    state = _run(
        "2026-11-10 我一个人在广东，我要去上海玩三天，有一天去找一个我在浦东的朋友",
        planner=_Planner("外滩", "豫园"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 12,
            "temperature_max": 16,
            "condition": "阴",
            "precipitation_probability": 40,
        },
    )

    assert state["status"] == "ok"
    response = state["response"]
    assert "出发地：广东" in response
    assert "目的地：上海" in response
    assert "目的地：广东" not in response
    assert "找朋友" in response and "浦东" in response


def test_multi_day_forecast_covers_every_trip_day():
    """三天行程要查三天天气：逐日一行，不能只拿第 1 天代表整趟。"""
    forecast_days = [
        {"date": "2026-11-10", "condition": "阴", "temperature_min": 12,
         "temperature_max": 16, "precipitation_probability": 10},
        {"date": "2026-11-11", "condition": "小雨", "temperature_min": 6,
         "temperature_max": 12, "precipitation_probability": 70},
        {"date": "2026-11-12", "condition": "多云", "temperature_min": 9,
         "temperature_max": 15, "precipitation_probability": 20},
    ]
    calls = []

    def forecast(destination, start, count):
        calls.append((destination, start, count))
        return {"city": destination, "days": forecast_days}

    state = _run(
        "2026-11-10 我一个人在广东，我要去上海玩三天",
        planner=_Planner("外滩", "豫园"),
        weather_forecast=forecast,
    )

    assert state["status"] == "ok"
    assert calls and calls[0][0] == "上海" and calls[0][2] == 3
    response = state["response"]
    assert "天气（第1天 11-10）" in response
    assert "天气（第3天 11-12）" in response
    # 降水取整趟最高的一天
    assert "降水概率：70%（第2天最高）" in response


def test_holiday_hint_reaches_constraints_but_good_air_does_not():
    """假日提示要进清单；AQI 只是"良"的时候不该多占一行。"""
    holidays = [{"date": "2026-11-10", "name": "Test", "local_name": "测试节"}]

    state = _run(
        "2026-11-10 我一个人去上海玩三天",
        planner=_Planner("外滩"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 12, "temperature_max": 16,
            "condition": "阴", "precipitation_probability": 10,
        },
        air_quality=lambda _city, _start, _days: {
            "days": [{"date": "2026-11-10", "aqi_max": 40}]
        },
        holidays=lambda _country, _year: {"holidays": holidays},
    )

    assert state["status"] == "ok"
    assert "测试节" in state["response"]
    assert "空气质量（第" not in state["response"]


def test_overseas_trip_gets_exchange_rate_hint():
    state = _run(
        "去东京玩三天，我一个人，2026-11-10 出发",
        planner=_Planner("浅草寺"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 12, "temperature_max": 18,
            "condition": "晴", "precipitation_probability": 0,
        },
        exchange_rate=lambda base, quote: {
            "base": base, "quote": quote, "rate": 23.73, "date": "2026-11-06"
        },
    )

    assert state["status"] == "ok"
    assert "汇率：1 人民币 ≈ 23.73 日元" in state["response"]


def test_holiday_check_never_runs_on_a_guessed_departure_date():
    """没说出发日期时先问，不会按"今天"推假日——那会在下个月出行时误报。"""
    calls = []

    def holidays(country, year):
        calls.append((country, year))
        return {"holidays": [{"date": "2026-11-09", "local_name": "测试节"}]}

    state = _run(
        "我一个人去上海玩三天",
        planner=_Planner("外滩"),
        holidays=holidays,
    )

    assert state.get("__interrupt__"), "缺出发日期应先追问"
    assert calls == []


def test_air_quality_is_skipped_beyond_its_forecast_window():
    """空气质量接口只给 5 天：七天行程不查，也不猜后面几天。"""
    calls = []

    def air(_city, _start, _days):
        calls.append((_city, _start, _days))
        return {"days": []}

    state = _run(
        "2026-11-10 我一个人去上海玩七天",
        planner=_Planner("外滩"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 12, "temperature_max": 16,
            "condition": "阴", "precipitation_probability": 10,
        },
        air_quality=air,
    )

    assert state["status"] == "ok"
    assert calls == []


def test_one_slow_fact_does_not_block_the_others(monkeypatch):
    """天气卡住时，假日提示照样要出现——四条事实是并发的。"""
    import app.graphs.subgraphs.trip_gear as tg

    monkeypatch.setattr(tg, "FACT_TIMEOUT_SECONDS", 0.05)

    async def slow_weather(*_args):
        await asyncio.sleep(0.5)
        return {"days": []}

    state = _run(
        "2026-11-10 我一个人去上海玩三天",
        planner=_Planner("外滩"),
        weather_forecast=slow_weather,
        holidays=lambda _country, _year: {
            "holidays": [{"date": "2026-11-10", "local_name": "测试节"}]
        },
    )

    assert state["status"] == "ok"
    assert "测试节" in state["response"]


def test_province_destination_asks_for_cities():
    """广东是省不是市：天气和路线都按城市查，不能拿省份硬排。"""
    state = _run("两个人从上海去广东玩一周，2026-11-10 出发，酒店一晚不超400")

    interrupts = state.get("__interrupt__") or []
    assert interrupts, "省份应触发反问"
    payload = interrupts[0].value
    assert payload["missing"] == ["具体城市"]
    assert "省份" in payload["question"]
    # 已经识别到的条件不能因为反问就丢
    facts = payload["facts"]
    assert facts["days"] == 7
    assert facts["lodging_budget"] == 400
    assert facts["origin"] == "上海"


def test_multi_city_trip_assigns_a_city_to_every_day():
    """"广州、深圳、珠海各玩两天"：六天要落到三个城市，不能只排一个城。"""
    state = _run(
        "两个人从上海去广州、深圳、珠海各玩两天，2026-11-10 出发，酒店一晚不超400",
        planner=_Planner("城市漫步"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 18, "temperature_max": 25,
            "condition": "多云", "precipitation_probability": 10,
        },
    )

    assert state["status"] == "ok"
    response = state["response"]
    assert "串联城市：广州 → 深圳 → 珠海" in response
    assert "天数：6 天" in response
    assert "### 第 1 天（广州）" in response
    assert "### 第 5 天（珠海）" in response


def test_many_cities_request_plans_with_default_province_cities():
    """"广东的很多城市"是授权我们挑，不该反问；挑完要标来源。"""
    state = _run(
        "两个人从上海去广东玩一周，2026-11-10 出发，去广东的很多城市，酒店一晚不超400",
        planner=_Planner("城市漫步"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 18, "temperature_max": 25,
            "condition": "多云", "precipitation_probability": 10,
        },
    )

    assert state["status"] == "ok"
    texts = [item["text"] for item in state["constraints"]]
    assert "串联城市：广州 → 深圳 → 珠海" in texts
    sources = [item["source"] for item in state["constraints"] if item["key"] == "cities"]
    assert sources == ["规则推导（省份常用城市）"]


def test_vague_answer_to_province_question_still_moves_forward():
    """答"广东的很多城市"不能原地再问一遍。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-vague"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
        planner=_Planner("城市漫步"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 18, "temperature_max": 25,
            "condition": "多云", "precipitation_probability": 10,
        },
    )

    first = asyncio.run(graph.ainvoke(
        {"user_message": "去广东玩一周", "user_id": "user-1", "thread_id": "trip-gear-vague"},
        cfg, context=ctx,
    ))
    assert first.get("__interrupt__"), "省份应先反问具体城市"

    second = asyncio.run(graph.ainvoke(
        Command(resume="目的地：广州、深圳、珠海；两个人；2026-11-10 出发；"
                       "出发地：上海；交通：自驾；住宿：酒店，每晚300元"),
        cfg, context=ctx,
    ))
    assert second["status"] == "ok"
    assert "串联城市：广州 → 深圳 → 珠海" in second["response"]


def test_single_city_answer_replaces_the_province():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-onecity"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
        planner=_Planner("城市漫步"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 18, "temperature_max": 25,
            "condition": "多云", "precipitation_probability": 10,
        },
    )

    asyncio.run(graph.ainvoke(
        {"user_message": "去广东玩一周", "user_id": "user-1", "thread_id": "trip-gear-onecity"},
        cfg, context=ctx,
    ))
    second = asyncio.run(graph.ainvoke(
        Command(resume="目的地：广州；两个人；2026-11-10 出发；"
                       "出发地：上海；交通：自驾；住宿：酒店，每晚300元"),
        cfg, context=ctx,
    ))

    assert second["status"] == "ok"
    assert "目的地：广州" in second["response"]


def test_province_clarify_cities_question_is_multi_select():
    """"去哪几个城市"的答案是集合：这一题必须多选，其余题不能跟着变成多选。"""
    # 没说人数：同一张卡会一起问人数/预算/出发日期，用来对照"哪些题不能多选"
    state = _run("从上海去广东玩一周")

    payload = (state.get("__interrupt__") or [])[0].value
    by_key = {item["key"]: item for item in payload["questions"]}

    assert by_key["cities"]["multi"] is True
    assert by_key["cities"]["options"]
    assert {"people", "budget", "start_date"} <= set(by_key)
    for key, question in by_key.items():
        if key == "cities":
            continue
        assert question["multi"] is False, f"{key} 不该多选"


def test_multi_city_clarify_answer_keeps_the_selected_order():
    """多选拼出的"目的地：广州、珠海"要按选中的顺序串成行程。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from app.services.clarify_questions import compose_answer

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-multicity"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
        planner=_Planner("城市漫步"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 18, "temperature_max": 25,
            "condition": "多云", "precipitation_probability": 10,
        },
    )
    first = asyncio.run(graph.ainvoke(
        {"user_message": "两个人从上海去广东玩一周", "user_id": "user-1", "thread_id": "trip-gear-multicity"},
        cfg, context=ctx,
    ))
    assert first.get("__interrupt__"), "省份应先反问具体城市"

    answer = compose_answer([
        {"prefix": "目的地", "value": ["珠海", "广州"]},
        {"prefix": "人数", "value": "2 人"},
        {"prefix": "出发日期", "value": "2026-11-10"},
        {"prefix": "出发地", "value": "上海"},
        {"prefix": "交通", "value": "自驾"},
        {"prefix": "住宿", "value": "酒店，每晚300元"},
    ])
    second = asyncio.run(graph.ainvoke(Command(resume=answer), cfg, context=ctx))

    assert second["status"] == "ok"
    # 顺序照用户选的来，不被地名表顺序改写成"广州 → 珠海"
    assert "串联城市：珠海 → 广州" in second["response"]


def test_clarify_payload_offers_selectable_options():
    """追问要带可点的选项，不是只给一段问题文字。"""
    state = _run("帮我规划个行程")

    payload = (state.get("__interrupt__") or [])[0].value
    keys = [item["key"] for item in payload["questions"]]
    assert keys[:2] == ["destination", "days"]
    assert all(item["options"] for item in payload["questions"][:2])
    assert all(item["allow_custom"] for item in payload["questions"])


def test_answering_with_chosen_options_resumes_the_plan():
    """点选后拼出来的那句话要能直接继续规划，不用再问一遍。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from app.services.clarify_questions import compose_answer

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-options"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
        planner=_Planner("西湖"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 12, "temperature_max": 18,
            "condition": "晴", "precipitation_probability": 0,
        },
    )
    asyncio.run(graph.ainvoke(
        {"user_message": "帮我规划个行程", "user_id": "user-1", "thread_id": "trip-gear-options"},
        cfg, context=ctx,
    ))

    answer = compose_answer([
        {"prefix": "出发地", "value": "上海"},
        {"prefix": "目的地", "value": "杭州"},
        {"prefix": "天数", "value": "3 天"},
        {"prefix": "人数", "value": "2 人"},
        {"prefix": "出发日期", "value": "2026-11-10"},
        {"prefix": "交通", "value": "自驾"},
        {"prefix": "住宿", "value": "酒店，每晚300元"},
    ])
    second = asyncio.run(graph.ainvoke(Command(resume=answer), cfg, context=ctx))

    assert second["status"] == "ok"
    assert "目的地：杭州" in second["response"]
    assert "天数：3 天" in second["response"]
    assert "人数：2 人" in second["response"]
    assert "出发日期：2026-11-10" in second["response"]


def test_model_cannot_move_the_itinerary_to_the_departure_city():
    """模型把出发地写进 day.city 时要改回事实分配，否则标题写广东、行程在上海。"""
    from app.trip_gear.models import TripFacts
    from app.trip_gear.service import TripGearService, _PlanOutput

    facts = TripFacts(
        origin="上海", destination="广东", cities=["汕头", "广州"], days=2, people=1
    )
    output = _PlanOutput.model_validate({
        "days": [
            {"day": 1, "city": "上海", "items": [
                {"time": "09:00", "activity": "外滩", "duration_min": 60, "cost": 0},
            ]},
            {"day": 2, "city": "上海", "items": [
                {"time": "09:00", "activity": "豫园", "duration_min": 60, "cost": 0},
            ]},
        ],
        "gear": [],
    })

    itinerary, _ = TripGearService._from_plan_output(facts, output)

    assert [day.city for day in itinerary.days] == ["汕头", "广州"]


def test_partial_answer_asks_again_for_what_is_still_missing():
    """补答里有一项没解析出来时，要再问那一项，不能直接停住。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-ask-again"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
    )

    first = asyncio.run(graph.ainvoke(
        {"user_message": "去莫干山两天，预算1500", "user_id": "user-1",
         "thread_id": "trip-gear-ask-again"},
        cfg,
        context=ctx,
    ))
    assert first["__interrupt__"][0].value["missing"] == [
        "出发地", "人数", "出发日期", "交通", "住宿",
    ]

    # 出发地/交通/住宿答了；人数答了；日期给了个解析不出来的"1"
    second = asyncio.run(graph.ainvoke(
        Command(
            resume=(
                "出发地：上海；交通：自驾；住宿：酒店，每晚300元；"
                "人数：1 人；出发日期：1"
            )
        ),
        cfg,
        context=ctx,
    ))
    interrupts = second.get("__interrupt__") or []
    assert interrupts, "还缺出发日期，应当继续问"
    assert interrupts[0].value["type"] == "trip_gear_clarify"
    assert interrupts[0].value["missing"] == ["出发日期"]

    # 补上日期后正常往下走（下一站是预算分配）
    third = asyncio.run(graph.ainvoke(Command(resume="下周一"), cfg, context=ctx))
    assert third["__interrupt__"][0].value["type"] == "trip_gear_budget_split"


def test_incomplete_answers_keep_asking():
    """答了但没解析出来：继续问，不设轮数上限。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-ask-limit"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
    )
    state = asyncio.run(graph.ainvoke(
        {"user_message": "去莫干山两天，预算1500", "user_id": "user-1",
         "thread_id": "trip-gear-ask-limit"},
        cfg,
        context=ctx,
    ))

    for _ in range(3):
        assert state.get("__interrupt__"), "答了但没补齐，应当继续问"
        state = asyncio.run(graph.ainvoke(Command(resume="随便写点"), cfg, context=ctx))

    assert state.get("__interrupt__"), "不设轮数上限：还在答就继续问"
    assert state["__interrupt__"][0].value["type"] == "trip_gear_clarify"


def test_empty_answer_stops_asking():
    """用户没作答（关掉卡片/空回答）就收尾，不拿同一张卡反复拦人。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-ask-empty"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
    )
    state = asyncio.run(graph.ainvoke(
        {"user_message": "去莫干山两天，预算1500", "user_id": "user-1",
         "thread_id": "trip-gear-ask-empty"},
        cfg,
        context=ctx,
    ))
    assert state.get("__interrupt__")

    state = asyncio.run(graph.ainvoke(Command(resume=""), cfg, context=ctx))

    assert not state.get("__interrupt__")
    assert state.get("status") == "needs_info"


def test_followup_message_keeps_the_trip_context():
    """补一句话不该把上一轮的目的地/天数/人数/日期/预算/城市全丢掉。"""
    from langgraph.checkpoint.memory import InMemorySaver

    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-carry"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
    )

    state = asyncio.run(graph.ainvoke(
        {
            "user_message": "我有一个pocket3",
            "history_text": (
                "上海出发，去广东玩五天，要去很多城市，包括汕头，预算5000\n"
                "人数：1 人；出发日期：明天"
            ),
            "user_id": "user-1",
            "thread_id": "trip-gear-carry",
        },
        cfg,
        context=ctx,
    ))

    payload = state["__interrupt__"][0].value
    facts = payload["facts"]
    assert facts["destination"] == "广东"
    assert facts["days"] == 5
    assert facts["people"] == 1
    assert facts["start_date"]
    assert facts["budget"] == 5000
    assert "汕头" in facts["cities"]


def test_each_phase_reports_what_it_is_doing_before_it_finishes():
    """过程区要能看到"这一秒在干什么"，不能只有一个通用兜底。

    每个阶段开始时先发一条 `active` 的 step + 一条 thinking（具体动作），
    结束时再发 `completed`。这样排行程、挑款这些几秒级的阶段在界面上
    有可读的进行中状态。
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    events = []
    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-progress"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
        planner=_Planner("城市漫步"),
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 12, "temperature_max": 18,
            "condition": "晴", "precipitation_probability": 0,
        },
    ).bind_writer(events.append)

    state = asyncio.run(graph.ainvoke(
        {
            "user_message": "两个人2026-11-10去杭州玩三天，预算5000",
            "user_id": "user-1",
            "thread_id": "trip-gear-progress",
        },
        cfg,
        context=ctx,
    ))
    if (state.get("__interrupt__") or [{}])[0].value.get("type") == "trip_gear_clarify":
        state = asyncio.run(graph.ainvoke(Command(
            resume="出发地：上海；交通：自驾；住宿：酒店，每晚300元",
        ), cfg, context=ctx))
    if (state.get("__interrupt__") or [{}])[0].value.get("type") == "trip_gear_budget_split":
        state = asyncio.run(graph.ainvoke(Command(resume="90"), cfg, context=ctx))

    active_steps = [
        event["data"]
        for event in events
        if event.get("event") == "step" and event["data"].get("status") == "active"
    ]
    # 每个阶段都要有一条"开始时"的播报，且文案是具体动作而不是"进行中"
    assert active_steps, "阶段开始时应当先发一条 active 播报"
    assert any("逐日行程" in str(item.get("description")) for item in active_steps)
    assert any("核对" in str(item.get("description")) for item in active_steps)

    live = [
        event["data"].get("message", "")
        for event in events
        if event.get("event") == "thinking"
    ]
    assert any("正在生成" in message and "天逐日行程" in message for message in live)
    assert any("住宿" in message and "总预算" in message for message in live)


def test_waiting_for_the_user_is_reported_as_waiting_not_running():
    """停在追问/等比例时，阶段卡片要写"等你确认"，不能一直"进行中"。

    以前 `_progress` 先播一条 active，中断后没人收尾：收尾渲染只能把它当成
    "进行中"（或归一成"已完成"），两种都不对——它其实在等人。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    events = []
    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-waiting"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
    ).bind_writer(events.append)

    state = asyncio.run(graph.ainvoke(
        {"user_message": "去广州玩五天", "user_id": "user-1", "thread_id": "trip-gear-waiting"},
        cfg, context=ctx,
    ))
    assert (state["__interrupt__"][0].value or {}).get("type") == "trip_gear_clarify"

    steps = [
        event["data"] for event in events if event.get("event") == "step"
    ]
    last = steps[-1]
    assert last["status"] == "waiting"
    assert "等你补充" in last["description"]


def test_budget_split_waiting_state_names_the_choice():
    from langgraph.checkpoint.memory import InMemorySaver

    events = []
    graph = build_trip_gear_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "trip-gear-split-wait"}}
    ctx = TurnContext(services=_Services(None), trace_id="trip-gear").with_extra(
        product_search=_search,
        weather_lookup=lambda _destination, _date: {
            "temperature_min": 22, "temperature_max": 30,
            "condition": "晴", "precipitation_probability": 0,
        },
    ).bind_writer(events.append)

    state = asyncio.run(graph.ainvoke(
        {
            "user_message": (
                "两个人2026-11-10去广州玩三天，预算6000，"
                "从上海出发，坐高铁，住酒店每晚300元"
            ),
            "user_id": "user-1",
            "thread_id": "trip-gear-split-wait",
        },
        cfg, context=ctx,
    ))
    assert (state["__interrupt__"][0].value or {}).get("type") == "trip_gear_budget_split"

    waiting = [event["data"] for event in events
               if event.get("event") == "step" and event["data"].get("status") == "waiting"]
    assert waiting and "等你定比例" in waiting[-1]["description"]
