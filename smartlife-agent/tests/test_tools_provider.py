"""免费真实天气/路线 provider 契约测试。"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.tools.map import MapInput, get_route
from app.tools.providers import ProviderUnavailable, get_provider_status
from app.tools.time import TimeInput
from app.tools.weather import WeatherInput, get_weather


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _geocode_payload(name):
    return {
        "results": [{
            "name": name,
            "latitude": 30.2741,
            "longitude": 120.1551,
            "timezone": "Asia/Shanghai",
        }]
    }


def _forecast_payload():
    target = date.today().isoformat()
    daily_times = [date.fromisoformat(target).isoformat()]
    daily_times.extend(["2099-01-01"] * 15)
    hourly_times = [f"{target}T00:00", f"{target}T01:00"]
    return {
        "current": {
            "temperature_2m": 24.4,
            "relative_humidity_2m": 45,
            "weather_code": 0,
        },
        "hourly": {
            "time": hourly_times,
            "relative_humidity_2m": [44, 46],
        },
        "daily": {
            "time": daily_times,
            "weather_code": [0] + [0] * 15,
            "temperature_2m_min": [18.0] * 16,
            "temperature_2m_max": [26.0] * 16,
            "precipitation_probability_max": [10] * 16,
        },
    }


def test_free_default_providers_require_no_configuration(monkeypatch):
    monkeypatch.delenv("SMARTLIFE_WEATHER_API_URL", raising=False)
    monkeypatch.delenv("SMARTLIFE_ROUTE_API_URL", raising=False)

    assert get_provider_status() == {"weather": True, "route": True}


def test_default_weather_uses_open_meteo_real_payload(monkeypatch):
    monkeypatch.delenv("SMARTLIFE_WEATHER_API_URL", raising=False)

    def respond(url, *args, **kwargs):
        if "geocoding-api.open-meteo.com" in url:
            return _Response(_geocode_payload("杭州"))
        return _Response(_forecast_payload())

    monkeypatch.setattr("app.tools.providers.requests.get", respond)

    result = get_weather.invoke({"city": "杭州", "date": date.today().isoformat()})

    assert result["simulated"] is False
    assert result["source"] == "https://api.open-meteo.com/v1/forecast"
    assert result["temperature"] == "24.4°C"
    assert result["condition"] == "晴"
    assert result["humidity"] == "45.0%"


def test_default_route_uses_openstreetmap_real_payload(monkeypatch):
    monkeypatch.delenv("SMARTLIFE_ROUTE_API_URL", raising=False)

    def respond(url, *args, **kwargs):
        if "geocoding-api.open-meteo.com" in url:
            return _Response(_geocode_payload("杭州"))
        assert "routing.openstreetmap.de/routed-car/" in url
        return _Response({
            "code": "Ok",
            "routes": [{"distance": 7630.7, "duration": 606.7}],
        })

    monkeypatch.setattr("app.tools.providers.requests.get", respond)

    result = get_route.invoke({
        "origin": "西湖",
        "destination": "灵隐寺",
        "mode": "driving",
        "city": "杭州",
    })

    assert result["simulated"] is False
    assert result["source"] == "https://routing.openstreetmap.de"
    assert result["distance"] == "7.6公里"
    assert result["duration"] == "10分钟"


def test_provider_failure_raises_instead_of_returning_simulated_data(monkeypatch):
    monkeypatch.delenv("SMARTLIFE_WEATHER_API_URL", raising=False)

    def fail(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.tools.providers.requests.get", fail)

    with pytest.raises(ProviderUnavailable):
        get_weather.invoke({"city": "杭州", "date": date.today().isoformat()})


def test_custom_weather_provider_is_used_when_configured(monkeypatch):
    monkeypatch.setenv("SMARTLIFE_WEATHER_API_URL", "https://weather.test/query")

    class WeatherResponse(_Response):
        def __init__(self):
            super().__init__({
                "temperature": "21°C",
                "condition": "晴",
                "humidity": "50%",
            })

    monkeypatch.setattr(
        "app.tools.providers.requests.get",
        lambda *args, **kwargs: WeatherResponse(),
    )

    result = get_weather.invoke({"city": "杭州", "date": date.today().isoformat()})

    assert result["source"] == "https://weather.test/query"
    assert result["simulated"] is False


def test_tool_input_schemas_reject_invalid_dates_modes_and_timezones():
    with pytest.raises(ValidationError):
        WeatherInput.model_validate({"city": "杭州", "date": "2026/10/05"})
    with pytest.raises(ValidationError):
        MapInput.model_validate({
            "origin": "西湖",
            "destination": "西湖",
            "mode": "driving",
        })
    with pytest.raises(ValidationError):
        MapInput.model_validate({
            "origin": "西湖",
            "destination": "灵隐寺",
            "mode": "transit",
        })
    with pytest.raises(ValidationError):
        TimeInput.model_validate({"timezone": "Mars/Olympus"})


def test_provider_cache_avoids_duplicate_requests(monkeypatch):
    monkeypatch.setenv("SMARTLIFE_WEATHER_API_URL", "https://cache.weather.test/query")
    calls = []

    def respond(*args, **kwargs):
        calls.append(args)
        return _Response({
            "temperature": "21°C",
            "condition": "晴",
            "humidity": "50%",
        })

    monkeypatch.setattr("app.tools.providers.requests.get", respond)

    first = get_weather.invoke({"city": "杭州", "date": date.today().isoformat()})
    second = get_weather.invoke({"city": "杭州", "date": date.today().isoformat()})

    assert len(calls) == 1
    assert first["cache_status"] == "miss"
    assert second["cache_status"] == "hit"


def test_provider_circuit_stops_repeated_calls_after_threshold(monkeypatch):
    from app.tools.providers import get_weather_provider

    monkeypatch.setenv("SMARTLIFE_WEATHER_API_URL", "https://circuit.weather.test/query")
    monkeypatch.setenv("SMARTLIFE_PROVIDER_FAILURE_THRESHOLD", "2")
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("down")

    monkeypatch.setattr("app.tools.providers.requests.get", fail)
    provider = get_weather_provider()

    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            provider.fetch({"city": "杭州", "date": date.today().isoformat()})
    with pytest.raises(ProviderUnavailable):
        provider.fetch({"city": "杭州", "date": date.today().isoformat()})

    assert len(calls) == 2
