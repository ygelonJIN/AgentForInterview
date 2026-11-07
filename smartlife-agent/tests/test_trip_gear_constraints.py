"""约束派生：目的地、周末、预算口径、天气派生。"""

from app.trip_gear.constraints import derive_constraints, ensure_start_date, enrich_weather


def test_departure_date_is_listed_as_a_constraint():
    facts, constraints = derive_constraints("下周六去莫干山露营两天", today="2026-11-07")

    entry = next(item for item in constraints if item.key == "start_date")
    assert facts.start_date == "2026-11-14"
    assert entry.text == "出发日期：2026-11-14"
    assert entry.source == "用户原话"


def test_missing_departure_date_defaults_to_today_and_says_so():
    facts, constraints = derive_constraints("去莫干山露营两天", today="2026-11-07")

    entry = next(item for item in constraints if item.key == "start_date")
    assert facts.start_date is None
    assert entry.text == "出发日期：未指定（按今天 2026-11-07 计）"
    assert entry.source == "默认规则"
    assert entry.hard is False


def test_start_date_placeholder_is_replaced_not_duplicated():
    facts, constraints = derive_constraints("去莫干山露营两天", today="2026-11-07")
    facts.start_date = "2026-11-20"

    constraints = ensure_start_date(facts, constraints, source="追问补充")

    entries = [item for item in constraints if item.key == "start_date"]
    assert [item.text for item in entries] == ["出发日期：2026-11-20"]
    assert entries[0].source == "追问补充"


def test_destination_outside_city_whitelist_is_derived():
    facts, constraints = derive_constraints(
        "周末两个人去莫干山露营，预算1500，我有帐篷和睡袋"
    )

    assert facts.destination == "莫干山"
    assert facts.days == 2  # 周末 -> 2 天
    assert facts.people == 2
    assert facts.lodging == "camp"
    assert facts.budget == 1500
    assert facts.budget_scope == "all_in"
    texts = [item.text for item in constraints]
    assert "目的地：莫干山" in texts
    assert "天数：2 天" in texts
    assert "预算口径：含装备" in texts


def test_budget_scope_can_be_excluded_explicitly():
    facts, constraints = derive_constraints("去杭州玩三天，预算3000，不含装备")

    assert facts.budget_scope == "trip_only"
    sources = {item.text: item.source for item in constraints}
    assert sources["预算口径：不含装备"] == "用户原话"


def test_separate_gear_budget_is_understood():
    facts, _constraints = derive_constraints("去临安露营两天，装备预算500")

    assert facts.budget_scope == "trip_only"
    assert facts.gear_budget == 500


def test_camping_and_cold_weather_derives_sleeping_bag_rating():
    facts, constraints = derive_constraints("周末去莫干山露营，预算1500")
    constraints = enrich_weather(
        facts,
        constraints,
        {
            "temperature_min": 8,
            "temperature_max": 18,
            "condition": "多云",
            "precipitation_probability": 10,
        },
    )

    texts = [item.text for item in constraints]
    assert "睡袋温标 ≤ 3℃" in texts
    assert facts.min_temp_c == 8
    assert any("夜间温度留 5℃ 余量" in item.source for item in constraints)


def test_weather_failure_is_a_soft_degradation_not_a_guess():
    facts, constraints = derive_constraints("周末去莫干山露营")
    constraints = enrich_weather(facts, constraints, None)

    degraded = [item for item in constraints if item.source == "降级"]
    assert len(degraded) == 1
    assert degraded[0].hard is False
    assert facts.min_temp_c is None
