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
    base = dict(
        origin="上海", destination="广东", cities=["汕头", "广州", "深圳"],
        days=7, people=2, budget=5000, transport="flight",
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


def test_camping_uses_person_night():
    amount, _ = lodging_estimate(_facts(lodging="camp"), 2)

    assert amount == pytest.approx(CAMP_PER_PERSON_NIGHT * 2 * 2)


def test_apply_estimates_charges_only_real_nights_and_adds_intercity():
    itinerary = apply_estimates(_itinerary(7), _facts())

    assert itinerary.days[-1].lodging_cost == 0
    assert itinerary.days[0].lodging_cost == pytest.approx(HOTEL_PER_ROOM_NIGHT)
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
