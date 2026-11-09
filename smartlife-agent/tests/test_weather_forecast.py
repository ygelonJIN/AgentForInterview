"""逐日天气：一次请求拿整段，逐天进约束清单，装备按整趟最坏值推导。

背景：以前一次性只查"出发那天"，第 2、3 天的天气从未被查询，清单里却看不出
这个缺口——用户以为整趟都查过了。
"""

from datetime import date, timedelta

import pytest

from app.tools.providers import get_weather_provider
from app.tools.weather import WeatherForecastInput, get_weather_forecast
from app.trip_gear.constraints import enrich_weather
from app.trip_gear.gear import derive_gear
from app.trip_gear.itinerary import fallback_itinerary
from app.trip_gear.models import TripFacts


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _geocode_payload(name="上海"):
    return {
        "results": [{
            "name": name,
            "latitude": 31.2222,
            "longitude": 121.4581,
            "timezone": "Asia/Shanghai",
            "country": "中国",
            "country_code": "CN",
            "feature_code": "PPLA",
            "population": 22000000,
        }]
    }


def _forecast_payload(mins, maxs, rains):
    """按给定序列造 16 天预报；只有前几天是真实连续日期。"""
    today = date.today()
    days = [(today + timedelta(days=offset)).isoformat() for offset in range(16)]
    return {
        "current": {"temperature_2m": 15.4, "relative_humidity_2m": 70, "weather_code": 3},
        "hourly": {
            "time": [f"{today.isoformat()}T0{hour}:00" for hour in range(2)],
            "relative_humidity_2m": [68, 72],
        },
        "daily": {
            "time": days,
            "weather_code": [3] * 16,
            "temperature_2m_min": list(mins) + [mins[-1]] * (16 - len(mins)),
            "temperature_2m_max": list(maxs) + [maxs[-1]] * (16 - len(maxs)),
            "precipitation_probability_max": list(rains) + [rains[-1]] * (16 - len(rains)),
        },
    }


@pytest.fixture
def three_day_payload():
    return _forecast_payload(mins=[15, 10, 6], maxs=[16, 15, 12], rains=[10, 60, 30])


def test_forecast_tool_returns_one_row_per_day(monkeypatch, three_day_payload):
    def respond(url, *args, **kwargs):
        if "geocoding-api.open-meteo.com" in url:
            return _Response(_geocode_payload())
        return _Response(three_day_payload)

    from app.tools import geo

    monkeypatch.delenv("SMARTLIFE_WEATHER_API_URL", raising=False)
    monkeypatch.setattr("app.tools.providers.requests.get", respond)
    monkeypatch.setattr("app.tools.geo.requests.get", respond)
    geo.reset_cache()

    result = get_weather_forecast.invoke({
        "city": "上海",
        "start_date": date.today().isoformat(),
        "days": 3,
    })

    assert result["source"] == "https://api.open-meteo.com/v1/forecast"
    assert len(result["days"]) == 3
    dates = [row["date"] for row in result["days"]]
    assert dates == [
        (date.today() + timedelta(days=offset)).isoformat() for offset in range(3)
    ]
    assert result["days"][2]["temperature_min"] == 6.0


def test_forecast_rejects_days_out_of_range():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WeatherForecastInput(city="上海", start_date=date.today().isoformat(), days=17)


def test_enrich_weather_writes_one_line_per_day():
    facts = TripFacts(destination="上海", days=3)
    weather = {
        "days": [
            {"date": (date.today() + timedelta(days=i)).isoformat(),
             "condition": condition,
             "temperature_min": mins,
             "temperature_max": maxs,
             "precipitation_probability": rain}
            for i, (condition, mins, maxs, rain) in enumerate(
                [("阴", 15, 16, 10), ("小雨", 10, 15, 60), ("多云", 6, 12, 30)]
            )
        ]
    }

    constraints = enrich_weather(facts, [], weather)
    weather_lines = [item.text for item in constraints if item.key == "weather"]

    assert len(weather_lines) == 3
    assert "第1天" in weather_lines[0] and "第3天" in weather_lines[2]
    assert "降水 60%" in weather_lines[1]
    # 整趟按最冷那天和最湿那天算，不按第 1 天拍板
    assert facts.min_temp_c == 6
    assert facts.rain_probability == 60
    assert "第2天" in facts.weather_summary


def test_gear_uses_coldest_and_wettest_day(three_day_payload):
    facts = TripFacts(destination="上海", days=3)
    enrich_weather(facts, [], {
        "days": [
            {"date": date.today().isoformat(), "condition": "阴",
             "temperature_min": 15, "temperature_max": 16, "precipitation_probability": 10},
            {"date": (date.today() + timedelta(days=1)).isoformat(), "condition": "小雨",
             "temperature_min": 3, "temperature_max": 15, "precipitation_probability": 60},
        ]
    })

    gear = derive_gear(facts, fallback_itinerary(facts))
    names = {item.name for item in gear}

    assert "羽绒服" in names  # 第 2 天降到 3℃，不能按第 1 天的 15℃ 判断
    assert "雨伞" in names


def test_enrich_weather_rewrites_instead_of_appending():
    """追问轮会再算一次天气：旧的逐日条目要撤掉，不能同一叠加两条。"""
    facts = TripFacts(destination="上海", days=2)
    rows = {
        "days": [
            {"date": "2026-11-10", "condition": "阴", "temperature_min": 10,
             "temperature_max": 15, "precipitation_probability": 10},
            {"date": "2026-11-11", "condition": "小雨", "temperature_min": 8,
             "temperature_max": 12, "precipitation_probability": 60},
        ]
    }

    constraints = enrich_weather(facts, [], rows)
    constraints = enrich_weather(facts, constraints, rows)

    assert len([item for item in constraints if item.key == "weather"]) == 2
    assert len([item for item in constraints if item.key == "rain"]) == 1
