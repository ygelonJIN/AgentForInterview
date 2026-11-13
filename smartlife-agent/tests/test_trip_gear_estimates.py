"""机票/住宿估算：把 5000 元真正算完整，而不是只算地面花费。"""

import pytest

from app.trip_gear.estimates import (
    CAMP_PER_PERSON_NIGHT,
    FLIGHT_PER_PERSON_LEG,
    HOTEL_PER_ROOM_NIGHT,
    RAIL_PER_PERSON_LEG,
    apply_estimates,
    intercity_estimate,
    lodging_estimate,
)
from app.trip_gear.itinerary import total_cost
from app.trip_gear.models import DayPlan, Itinerary, ItineraryItem, TripFacts


def _facts(**kwargs):
    # 每晚预算必须写成"用户说过"的：住宿不再有 300 的默认值，
    # 没给预算就不估算（见 test_lodging_without_nightly_budget_is_not_estimated）。
    base = dict(
        origin="上海", destination="广东", cities=["汕头", "广州", "深圳"],
        days=7, people=2, budget=5000, transport="flight", lodging="hotel",
        lodging_budget=HOTEL_PER_ROOM_NIGHT,
    )
    base.update(kwargs)
    return TripFacts(**base)


def _itinerary(days=7):
    return Itinerary(
        destination="广东",
        days=[
            DayPlan(day=i + 1, items=[ItineraryItem(time="10:00", activity="活动", cost=100)])
            for i in range(days)
        ],
    )


def test_flight_round_trip_plus_intra_city_rail():
    amount, notes = intercity_estimate(_facts())

    assert amount == pytest.approx(2 * FLIGHT_PER_PERSON_LEG * 2 + 2 * RAIL_PER_PERSON_LEG * 2)
    assert "机票 2 段" in notes[0]
    assert "城际高铁 2 段" in notes[0]


def test_train_trip_counts_round_trip_and_between_cities():
    amount, _ = intercity_estimate(_facts(transport="train"))

    assert amount == pytest.approx(4 * RAIL_PER_PERSON_LEG * 2)


def test_lodging_uses_room_night_and_six_nights():
    amount, notes = lodging_estimate(_facts(), 6)

    assert amount == pytest.approx(HOTEL_PER_ROOM_NIGHT * 1 * 6)
    assert "6 晚" in notes[0]


def test_lodging_without_nightly_budget_is_not_estimated():
    """没说每晚预算就不估算：不拿 300 经济型替用户编一个数字。"""
    amount, notes = lodging_estimate(_facts(lodging="hotel", lodging_budget=None), 6)

    assert amount == 0.0
    assert "未计入总预算" in notes[0]


def test_camping_uses_person_night():
    amount, _ = lodging_estimate(_facts(lodging="camp"), 2)

    assert amount == pytest.approx(CAMP_PER_PERSON_NIGHT * 2 * 2)


def test_apply_estimates_charges_only_real_nights_and_adds_intercity():
    itinerary = apply_estimates(_itinerary(7), _facts())

    assert itinerary.days[-1].lodging_cost == 0
    assert itinerary.days[0].lodging_cost == pytest.approx(HOTEL_PER_ROOM_NIGHT)  # 用户说的一晚价
    assert itinerary.intercity_cost == pytest.approx(3680)
    # 条目 700 + 住宿 6×300 + 城际 3680
    assert total_cost(itinerary) == pytest.approx(700 + 1800 + 3680)


def test_one_day_trip_has_no_lodging():
    itinerary = apply_estimates(_itinerary(1), _facts(days=1))

    assert itinerary.days[0].lodging_cost == 0


def test_panel_marks_estimates_and_includes_intercity_in_total():
    from app.ui.theme import plan_panels_html

    itinerary = apply_estimates(_itinerary(2), _facts(days=2))
    html = plan_panels_html({
        "facts": {"budget": 5000, "destination": "广东", "people": 2,
                  "budget_scope": "all_in"},
        "itinerary": itinerary.model_dump(),
        "gear": [], "products": {}, "issues": [], "constraints": [],
    })

    assert "含城际估算 ¥3680" in html
    assert "住宿估算" in html
    assert "机票 2 段" in html


def test_model_itemized_transport_is_not_counted_twice():
    """模型把大交通写成当天条目时，不再叠一层总额估算（否则同一笔钱算两遍）。"""
    itinerary = _itinerary(2)
    itinerary.days[0].items.append(
        ItineraryItem(time="08:00", activity="高铁 上海→广州（2 人）", cost=480)
    )
    result = apply_estimates(itinerary, _facts(transport="train"), trust_items=True)

    assert result.intercity_cost == 0.0
    assert any("条目" in note for note in result.estimate_notes)


def test_model_lodging_wins_over_the_rule_estimate():
    itinerary = _itinerary(2)
    itinerary.days[0].lodging_cost = 520
    result = apply_estimates(itinerary, _facts(), trust_items=True)

    assert result.days[0].lodging_cost == 520
    assert any("模型估算" in note for note in result.estimate_notes)


def test_daily_itinerary_shows_lodging_and_intercity_rows():
    """逐日行程里必须能看到住宿和城际交通：它们占了总预算的一大半。"""
    from app.ui.theme import plan_panels_html

    itinerary = _itinerary(3)
    itinerary.days[0].items.append(
        ItineraryItem(time="08:00", activity="西湖", cost=100)
    )
    itinerary = apply_estimates(itinerary, _facts(days=3))
    html = plan_panels_html({
        "facts": {"budget": 5000, "destination": "广东", "people": 2,
                  "budget_scope": "all_in"},
        "itinerary": itinerary.model_dump(),
        "gear": [], "products": {}, "issues": [], "constraints": [],
    })

    assert "住宿<small>酒店 · 估算</small>" in html
    assert "城际交通<small>机票/高铁 · 估算</small>" in html
    # 住宿只算真实过夜的 2 晚（3 天 → 2 晚），最后一天没有住宿行
    assert html.count("住宿<small>") == 2


def test_transport_items_without_prices_fall_back_to_the_estimate():
    """模型写了"机票 上海→广州"却没写金额：按单价补上，别让大交通整块消失。"""
    itinerary = _itinerary(2)
    itinerary.days[0].items.append(
        ItineraryItem(time="08:00", activity="机票 上海→广州（2 人）", cost=0)
    )

    result = apply_estimates(itinerary, _facts(transport="flight"), trust_items=True)

    assert result.intercity_cost > 0
    assert any("没有金额" in note for note in result.estimate_notes)
