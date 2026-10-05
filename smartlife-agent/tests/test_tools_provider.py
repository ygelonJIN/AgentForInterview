"""外部天气/路线 provider 配置与降级测试。"""

from types import SimpleNamespace

from app.tools.map import get_route
from app.tools.providers import get_provider_status
from app.tools.hotel import search_hotels
from app.tools.weather import get_weather


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"temperature": "21°C", "condition": "晴", "distance": "3.0公里"}


def test_provider_status_reports_unconfigured_services(monkeypatch):
    monkeypatch.delenv("SMARTLIFE_WEATHER_API_URL", raising=False)
    monkeypatch.delenv("SMARTLIFE_ROUTE_API_URL", raising=False)
    monkeypatch.delenv("SMARTLIFE_HOTEL_API_URL", raising=False)

    assert get_provider_status() == {"weather": False, "route": False, "hotel": False}


def test_weather_provider_is_used_when_configured(monkeypatch):
    monkeypatch.setenv("SMARTLIFE_WEATHER_API_URL", "https://weather.test/query")
    monkeypatch.setattr("app.tools.providers.requests.get", lambda *args, **kwargs: _Response())

    result = get_weather.invoke({"city": "杭州", "date": "2026-10-05"})

    assert result["simulated"] is False
    assert result["source"] == "https://weather.test/query"


def test_route_provider_falls_back_to_simulated_result_on_error(monkeypatch):
    monkeypatch.setenv("SMARTLIFE_ROUTE_API_URL", "https://route.test/query")

    def fail(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.tools.providers.requests.get", fail)

    result = get_route.invoke({"origin": "西湖", "destination": "灵隐寺", "mode": "driving"})

    assert result["simulated"] is True
    assert result["source"] == "local_mock_map"


def test_hotel_provider_and_simulated_fallback(monkeypatch):
    monkeypatch.delenv("SMARTLIFE_HOTEL_API_URL", raising=False)
    fallback = search_hotels.invoke({
        "city": "杭州",
        "check_in": "2026-10-05",
        "check_out": "2026-10-07",
    })
    assert fallback["simulated"] is True
    assert fallback["source"] == "local_mock_hotel"

    monkeypatch.setenv("SMARTLIFE_HOTEL_API_URL", "https://hotel.test/search")

    class HotelResponse(_Response):
        def json(self):
            return {"hotels": [{"id": "real-hotel", "name": "真实酒店"}]}

    monkeypatch.setattr("app.tools.providers.requests.get", lambda *args, **kwargs: HotelResponse())
    external = search_hotels.invoke({
        "city": "杭州",
        "check_in": "2026-10-05",
        "check_out": "2026-10-07",
    })

    assert external["simulated"] is False
    assert external["hotels"][0]["id"] == "real-hotel"
