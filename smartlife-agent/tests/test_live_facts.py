"""实时外部事实：空气质量、汇率、节假日。

这些工具和天气同性质——查回来的是外部事实，直接进约束清单或装备规则，
不问模型意见。拿不到就降级，不猜。
"""

from types import SimpleNamespace

from app.tools.travel_facts import (
    CURRENCY_BY_PLACE,
    currency_for,
    get_exchange_rate,
    get_public_holidays,
    holidays_in_range,
)
from app.tools.weather import get_air_quality
from app.trip_gear.constraints import (
    derive_constraints,
    enrich_air_quality,
    enrich_exchange_rate,
    enrich_holidays,
    enrich_weather,
)
from app.trip_gear.gear import derive_gear
from app.trip_gear.itinerary import fallback_itinerary
from app.trip_gear.models import TripFacts


def _response(payload, url="https://example.test/api"):
    return SimpleNamespace(
        payload=payload,
        cache_status="miss",
        fetched_at=1.0,
        stale=False,
        warnings=(),
    )


class _FakeProvider:
    def __init__(self, payload, url="https://example.test/api"):
        self._payload = payload
        self.url = url

    def fetch_with_meta(self, params, **_kwargs):
        return _response(self._payload, self.url)


# ---------------------------------------------------------------- 空气质量


def test_air_quality_tool_returns_daily_rows(monkeypatch):
    payload = {
        "city": "上海",
        "start": "2026-11-10",
        "standard": "us_aqi",
        "days": [
            {"date": "2026-11-10", "pm2_5_max": 35.0, "aqi_max": 84.0},
            {"date": "2026-11-11", "pm2_5_max": 72.0, "aqi_max": 132.0},
        ],
    }
    monkeypatch.setattr(
        "app.tools.weather.get_air_quality_provider",
        lambda: _FakeProvider(payload),
    )

    result = get_air_quality.invoke({
        "city": "上海",
        "start_date": "2026-11-10",
        "days": 2,
    })

    assert result["simulated"] is False
    assert result["standard"] == "us_aqi"
    assert [row["aqi_max"] for row in result["days"]] == [84.0, 132.0]


def test_bad_air_only_shows_up_when_it_is_bad():
    facts = TripFacts(destination="上海", days=2)
    air = {
        "days": [
            {"date": "2026-11-10", "aqi_max": 84},
            {"date": "2026-11-11", "aqi_max": 132},
        ]
    }

    constraints = enrich_air_quality(facts, [], air)
    lines = [item.text for item in constraints if item.key == "air_quality"]

    assert len(lines) == 1
    assert "第2天" in lines[0] and "轻度污染" in lines[0]
    assert facts.aqi_max == 132


def test_missing_air_quality_adds_no_noise():
    """补充证据拿不到就不写条目；降级说明由天气那条负责。"""
    facts = TripFacts(destination="上海", days=2)

    constraints = enrich_air_quality(facts, [], None)

    assert constraints == []
    assert facts.aqi_max is None


# ---------------------------------------------------------------- 汇率


def test_currency_map_covers_overseas_places():
    assert currency_for("东京") == ("JPY", "日元")
    assert currency_for("巴黎") == ("EUR", "欧元")
    assert currency_for("上海") is None  # 境内不写汇率
    assert currency_for("某个表外海岛") is None


def test_exchange_rate_is_a_soft_constraint():
    facts = TripFacts(destination="东京", days=3)

    constraints = enrich_exchange_rate(
        facts,
        [],
        {"rate": 23.73, "date": "2026-11-06"},
        ("JPY", "日元"),
    )

    assert len(constraints) == 1
    assert constraints[0].hard is False
    assert "23.73" in constraints[0].text
    assert "2026-11-06" in constraints[0].text


def test_domestic_trip_has_no_exchange_rate():
    facts = TripFacts(destination="上海", days=3)

    constraints = enrich_exchange_rate(facts, [], {"rate": 1.0}, None)

    assert constraints == []


# ---------------------------------------------------------------- 节假日


def test_holiday_hint_only_for_dates_in_trip():
    holidays = [
        {"date": "2026-10-01", "name": "National Day", "local_name": "国庆节"},
        {"date": "2026-12-25", "name": "Christmas", "local_name": "圣诞节"},
    ]
    assert [item["date"] for item in holidays_in_range(
        holidays, ["2026-10-01", "2026-10-02"]
    )] == ["2026-10-01"]

    hits = holidays_in_range(holidays, ["2026-10-01"])
    constraints = enrich_holidays(TripFacts(), [], hits)

    assert constraints[0].key == "holiday"
    assert "国庆节" in constraints[0].text
    assert constraints[0].hard is False


# ---------------------------------------------------------------- 紫外线


def test_uv_constraint_and_sunscreen_gear():
    facts = TripFacts(destination="上海", days=2)
    weather = {
        "days": [
            {"date": "2026-11-10", "condition": "晴", "temperature_min": 15,
             "temperature_max": 20, "precipitation_probability": 0,
             "sunrise": "2026-11-10T06:22", "sunset": "2026-11-10T17:03",
             "uv_index_max": 7},
            {"date": "2026-11-11", "condition": "晴", "temperature_min": 14,
             "temperature_max": 19, "precipitation_probability": 0,
             "sunrise": "2026-11-11T06:23", "sunset": "2026-11-11T17:02",
             "uv_index_max": 9},
        ]
    }

    constraints = enrich_weather(facts, [], weather)
    texts = [item.text for item in constraints]

    assert facts.uv_index_max == 9
    assert any("紫外线：峰值 9" in text for text in texts)
    assert any("日出 06:22 / 日落 17:03" in text for text in texts)

    gear = {item.name for item in derive_gear(facts, fallback_itinerary(facts))}
    assert "防晒霜" in gear and "墨镜" in gear


def test_bad_air_adds_mask_to_gear():
    facts = TripFacts(destination="上海", days=2, aqi_max=180)

    gear = {item.name: item for item in derive_gear(facts, fallback_itinerary(facts))}

    assert "口罩" in gear
    assert "180" in gear["口罩"].reason


# ---------------------------------------------------------------- 链路


def test_overseas_trip_keeps_weather_and_fx_apart():
    """天气、空气质量、汇率、假日各是各的约束，互不覆盖。"""
    facts, constraints = derive_constraints("去东京玩三天，预算8000")
    constraints = enrich_weather(facts, constraints, [
        {"date": "2026-11-10", "condition": "晴", "temperature_min": 12,
         "temperature_max": 18, "precipitation_probability": 0, "uv_index_max": 5},
    ])
    constraints = enrich_air_quality(facts, constraints, {"days": [
        {"date": "2026-11-10", "aqi_max": 40},
    ]})
    constraints = enrich_exchange_rate(
        facts, constraints, {"rate": 23.73, "date": "2026-11-06"}, ("JPY", "日元")
    )

    keys = {item.key for item in constraints}
    assert {"weather", "fx", "destination"} <= keys
    assert "air_quality" not in keys  # AQI 40 是优，不写条目
