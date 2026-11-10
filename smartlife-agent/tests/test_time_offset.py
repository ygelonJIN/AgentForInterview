"""时差工具与 travel 无装备模式的回归测试。"""

from datetime import date

from app.trip_gear.constraints import enrich_timezone
from app.trip_gear.itinerary import fallback_itinerary
from app.trip_gear.models import TripFacts
from app.trip_gear.verify import verify
from app.tools.time_offset import (
    describe_offset,
    get_time_offset,
    resolve_zone,
    time_difference,
)


def test_resolve_zone_prefers_city_then_country():
    zone, precision = resolve_zone("纽约")
    assert (zone, precision) == ("America/New_York", "city")

    zone, precision = resolve_zone("东京")
    assert (zone, precision) == ("Asia/Tokyo", "country")


def test_unknown_city_is_not_guessed():
    payload = time_difference("亚特兰蒂斯")
    assert payload["available"] is False
    assert "reason" in payload
    assert "timezone" not in payload or not payload.get("timezone")


def test_time_offset_includes_dst_and_beijing_diff():
    summer = time_difference("纽约", date(2026, 7, 15))
    winter = time_difference("纽约", date(2026, 1, 15))

    assert summer["utc_offset"] == -4.0
    assert winter["utc_offset"] == -5.0
    assert winter["beijing_diff_hours"] == -13.0
    assert describe_offset(-5.0) == "UTC-05:00"


def test_tool_surface_returns_local_time():
    payload = get_time_offset.invoke({"city": "东京", "date": "2026-11-15"})

    assert payload["available"] is True
    assert payload["timezone"] == "Asia/Tokyo"
    assert payload["local_time"]


def test_enrich_timezone_writes_only_when_different():
    tokyo = TripFacts(destination="东京", days=3)
    constraints = enrich_timezone(tokyo, [], time_difference("东京", date(2026, 11, 15)))
    assert [item.key for item in constraints] == ["timezone"]
    assert "早 1 小时" in constraints[0].text
    assert tokyo.time_diff_hours == 1.0

    sanya = TripFacts(destination="三亚", days=3)
    assert enrich_timezone(sanya, [], time_difference("三亚")) == []
    assert sanya.time_diff_hours == 0.0


def test_travel_verification_skips_gear_checks():
    facts = TripFacts(destination="莫干山", days=2, people=2, lodging="camp")
    itinerary = fallback_itinerary(facts)

    with_gear = verify(facts, [], itinerary, [], {})
    without = verify(facts, [], itinerary, [], {}, with_gear=False)

    assert any("露营装备" in issue.constraint for issue in with_gear)
    assert not any("露营装备" in issue.constraint for issue in without)
