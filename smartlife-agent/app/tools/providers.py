"""外部工具 provider 配置与 HTTP 适配器。"""
import os
from typing import Any, Dict

import requests


class ProviderUnavailable(RuntimeError):
    """没有配置外部 provider。"""


class JsonHttpProvider:
    def __init__(self, name: str, url: str, api_key: str = ""):
        self.name = name
        self.url = url
        self.api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def fetch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.url:
            raise ProviderUnavailable(f"{self.name} 未配置 URL")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = requests.get(self.url, params=params, headers=headers, timeout=8)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"{self.name} 返回格式不是 JSON 对象")
        return payload


def get_weather_provider() -> JsonHttpProvider:
    return JsonHttpProvider(
        name="weather",
        url=os.environ.get("SMARTLIFE_WEATHER_API_URL", ""),
        api_key=os.environ.get("SMARTLIFE_WEATHER_API_KEY", ""),
    )


def get_route_provider() -> JsonHttpProvider:
    return JsonHttpProvider(
        name="route",
        url=os.environ.get("SMARTLIFE_ROUTE_API_URL", ""),
        api_key=os.environ.get("SMARTLIFE_ROUTE_API_KEY", ""),
    )


def get_hotel_provider() -> JsonHttpProvider:
    return JsonHttpProvider(
        name="hotel",
        url=os.environ.get("SMARTLIFE_HOTEL_API_URL", ""),
        api_key=os.environ.get("SMARTLIFE_HOTEL_API_KEY", ""),
    )


def get_provider_status() -> Dict[str, bool]:
    return {
        "weather": get_weather_provider().configured,
        "route": get_route_provider().configured,
        "hotel": get_hotel_provider().configured,
    }
