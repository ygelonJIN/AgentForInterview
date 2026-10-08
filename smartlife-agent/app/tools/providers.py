"""真实外部工具 provider 配置、HTTP 适配、缓存和熔断。

天气默认使用 Open-Meteo，路线默认使用 OpenStreetMap 公共路线服务。
这些默认服务免费且无需 API Key，但属于公共公平使用服务，不提供 SLA。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

from app.reliability import CircuitBreaker, TTLCache


class ProviderUnavailable(RuntimeError):
    """Provider 未配置、熔断或调用失败。"""


@dataclass(frozen=True)
class ProviderResponse:
    payload: Dict[str, Any]
    cache_status: str
    fetched_at: float
    stale: bool
    warnings: tuple[str, ...] = ()


_CIRCUIT_BREAKERS: Dict[str, CircuitBreaker] = {}
_RESPONSE_CACHES: Dict[str, TTLCache[Dict[str, Any]]] = {}
_GEOCODE_CACHE: TTLCache[Dict[str, Any]] = TTLCache(max_entries=256, ttl_seconds=24 * 60 * 60)


def _epoch_from_monotonic(created_at: float) -> float:
    return time.time() - max(0.0, time.monotonic() - created_at)


def _cache_ttl(name: str) -> float:
    default = "60" if name == "weather-open-meteo" else "30"
    try:
        return max(0.0, float(os.environ.get("SMARTLIFE_PROVIDER_CACHE_TTL", default)))
    except ValueError:
        return float(default)


def _cache(name: str, url: str) -> TTLCache[Dict[str, Any]]:
    key = f"{name}:{url}"
    cache = _RESPONSE_CACHES.get(key)
    ttl = _cache_ttl(name)
    if cache is None or cache.ttl_seconds != ttl:
        cache = TTLCache(max_entries=128, ttl_seconds=ttl)
        _RESPONSE_CACHES[key] = cache
    return cache


def _breaker(name: str, url: str) -> CircuitBreaker:
    key = f"{name}:{url}"
    breaker = _CIRCUIT_BREAKERS.get(key)
    if breaker is None:
        breaker = CircuitBreaker(
            key,
            failure_threshold=int(os.environ.get("SMARTLIFE_PROVIDER_FAILURE_THRESHOLD", "3")),
            recovery_timeout_seconds=float(os.environ.get("SMARTLIFE_PROVIDER_RECOVERY_SECONDS", "30")),
        )
        _CIRCUIT_BREAKERS[key] = breaker
    return breaker


def reset_provider_runtime_state() -> None:
    global _GEOCODE_CACHE
    _CIRCUIT_BREAKERS.clear()
    _RESPONSE_CACHES.clear()
    _GEOCODE_CACHE = TTLCache(max_entries=256, ttl_seconds=24 * 60 * 60)


def get_provider_diagnostics() -> Dict[str, Any]:
    return {
        "breakers": [breaker.snapshot() for breaker in _CIRCUIT_BREAKERS.values()],
        "caches": {key: cache.stats() for key, cache in _RESPONSE_CACHES.items()},
        "geocode_cache": _GEOCODE_CACHE.stats(),
    }


class JsonHttpProvider:
    """可选的自定义 JSON HTTP provider。"""

    def __init__(self, name: str, url: str, api_key: str = ""):
        self.name = name
        self.url = url
        self.api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _cache_key(self, params: Dict[str, Any]) -> str:
        payload = {"name": self.name, "url": self.url, "params": params}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _fetch_once(self, params: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = requests.get(self.url, params=params, headers=headers, timeout=8)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"{self.name} 返回格式不是 JSON 对象")
        return payload

    def fetch_with_meta(
        self,
        params: Dict[str, Any],
        *,
        cache_ttl_seconds: Optional[float] = None,
        allow_stale: bool = False,
    ) -> ProviderResponse:
        if not self.url:
            raise ProviderUnavailable(f"{self.name} 未配置 URL")
        cache = _cache(self.name, self.url)
        key = self._cache_key(params)
        if cache_ttl_seconds is not None and cache_ttl_seconds != cache.ttl_seconds:
            cache = TTLCache(max_entries=128, ttl_seconds=cache_ttl_seconds)
            _RESPONSE_CACHES[f"{self.name}:{self.url}"] = cache

        cached = cache.get(key)
        if cached is not None:
            return ProviderResponse(
                payload=cached.value,
                cache_status="hit",
                fetched_at=_epoch_from_monotonic(cached.created_at),
                stale=not cached.fresh,
            )

        breaker = _breaker(self.name, self.url)
        try:
            payload = breaker.call(lambda: self._fetch_once(params))
        except Exception as exc:
            stale = cache.get(key, allow_stale=True) if allow_stale else None
            if stale is not None:
                return ProviderResponse(
                    payload=stale.value,
                    cache_status="stale_fallback",
                    fetched_at=_epoch_from_monotonic(stale.created_at),
                    stale=True,
                    warnings=(f"{self.name} provider 调用失败，使用过期缓存: {type(exc).__name__}",),
                )
            if isinstance(exc, ProviderUnavailable):
                raise
            raise ProviderUnavailable(f"{self.name} provider 调用失败: {type(exc).__name__}: {exc}") from exc

        cache.put(key, payload)
        return ProviderResponse(
            payload=payload,
            cache_status="miss",
            fetched_at=time.time(),
            stale=False,
        )

    def fetch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.fetch_with_meta(params).payload


OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OSRM_ROUTING_BASE_URL = "https://routing.openstreetmap.de"
OSRM_PUBLIC_FALLBACK_URL = "https://router.project-osrm.org"
NOMINATIM_GEOCODING_URL = "https://nominatim.openstreetmap.org/search"


def _request_json(
    url: str,
    params: Dict[str, Any],
    *,
    timeout: float = 10.0,
    attempts: int = 3,
) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(max(1, attempts)):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"User-Agent": "smartlife-agent/1.0"},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                payload = {"results": payload}
            if not isinstance(payload, dict):
                raise ValueError("外部服务返回格式不是 JSON 对象或数组")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(f"外部服务重试后仍失败: {last_error}") from last_error


def _geocode_candidates(query: str) -> List[str]:
    normalized = " ".join(str(query).replace("，", ",").split())
    candidates = [normalized, normalized.replace(",", " ")]
    if "," in normalized:
        place, _, city = normalized.partition(",")
        place = place.strip()
        city = city.strip()
        if place and city:
            candidates.extend((f"{city}{place}", f"{place} {city}"))
    return list(dict.fromkeys(item for item in candidates if item.strip()))


def _geocode(query: str) -> Dict[str, Any]:
    cache_key = " ".join(str(query).split()).casefold()
    cached = _GEOCODE_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached.value)

    results: list[Dict[str, Any]] = []
    for candidate in _geocode_candidates(query):
        try:
            payload = _request_json(
                OPEN_METEO_GEOCODING_URL,
                {"name": candidate, "count": 1, "language": "zh", "format": "json"},
                attempts=1,
            )
            results = payload.get("results") or []
        except Exception:
            results = []
        if results:
            break
        try:
            payload = _request_json(
                NOMINATIM_GEOCODING_URL,
                {
                    "q": candidate,
                    "countrycodes": "cn",
                    "format": "jsonv2",
                    "limit": 1,
                },
                attempts=1,
            )
            results = payload.get("results") or payload or []
        except Exception:
            results = []
        if results:
            break
    if not results:
        raise ValueError(f"无法定位地点: {query}")
    result = results[0]
    latitude = result.get("latitude", result.get("lat"))
    longitude = result.get("longitude", result.get("lon"))
    if latitude is None or longitude is None:
        raise ValueError(f"地理编码结果缺少坐标: {query}")
    normalized_result = {
        "name": result.get("name") or str(result.get("display_name", query)).split(",")[0],
        "latitude": float(latitude),
        "longitude": float(longitude),
        "timezone": result.get("timezone", "auto"),
    }
    _GEOCODE_CACHE.put(cache_key, normalized_result)
    return copy.deepcopy(normalized_result)


def _location_query(query: str, city: Optional[str] = None) -> str:
    query = str(query).strip()
    city = str(city or "").strip()
    if city and city not in query:
        return f"{query}, {city}"
    return query


_WMO_CONDITIONS = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "强阵雨",
    82: "暴阵雨",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷暴伴冰雹",
}


class OpenMeteoWeatherProvider(JsonHttpProvider):
    """Open-Meteo 免费、无 API Key 的真实天气 provider。"""

    def __init__(self):
        super().__init__(
            name="weather-open-meteo",
            url=OPEN_METEO_FORECAST_URL,
        )

    def _fetch_once(self, params: Dict[str, Any]) -> Dict[str, Any]:
        city = str(params["city"]).strip()
        target = date.fromisoformat(str(params["date"]))
        today = date.today()
        if target < today or target > today + timedelta(days=15):
            raise ValueError("Open-Meteo 预报仅支持今天至未来 15 天")

        location = _geocode(city)
        forecast = _request_json(
            OPEN_METEO_FORECAST_URL,
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "hourly": "relative_humidity_2m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "forecast_days": 16,
                "timezone": "auto",
            },
        )
        daily = forecast.get("daily") or {}
        times = daily.get("time") or []
        if target.isoformat() not in times:
            raise ValueError("Open-Meteo 未返回目标日期的预报")
        index = times.index(target.isoformat())

        current = forecast.get("current") or {}
        is_today = target == today
        daily_codes = daily.get("weather_code") or []
        weather_code = int(
            current.get("weather_code", daily_codes[index])
            if is_today
            else daily_codes[index]
        )
        temperature_min = float(daily.get("temperature_2m_min", [0.0])[index])
        temperature_max = float(daily.get("temperature_2m_max", [0.0])[index])
        precipitation = daily.get("precipitation_probability_max", [None])[index]
        hourly = forecast.get("hourly") or {}
        hourly_times = hourly.get("time") or []
        humidity_values = [
            float(value)
            for timestamp, value in zip(hourly_times, hourly.get("relative_humidity_2m") or [])
            if str(timestamp).startswith(target.isoformat()) and value is not None
        ]
        humidity = (
            round(sum(humidity_values) / len(humidity_values), 1)
            if humidity_values
            else current.get("relative_humidity_2m", "")
        )
        if is_today:
            temperature = f"{float(current.get('temperature_2m', temperature_max)):.1f}°C"
        else:
            temperature = f"{temperature_min:.1f}~{temperature_max:.1f}°C"

        condition = _WMO_CONDITIONS.get(weather_code, f"WMO {weather_code}")
        suggestion = (
            "建议携带雨具"
            if weather_code >= 51 or (precipitation or 0) >= 50
            else "适合户外活动"
        )
        return {
            "city": location["name"],
            "date": target.isoformat(),
            "temperature": temperature,
            "temperature_min": temperature_min,
            "temperature_max": temperature_max,
            "condition": condition,
            "humidity": f"{humidity}%",
            "precipitation_probability": precipitation,
            "suggestion": suggestion,
            "latitude": location["latitude"],
            "longitude": location["longitude"],
        }


class OpenStreetMapRouteProvider(JsonHttpProvider):
    """OpenStreetMap 公共路线 provider，支持驾车和步行。"""

    _PROFILES = {
        "driving": ("routed-car", "driving"),
        "walking": ("routed-foot", "foot"),
    }

    def __init__(self):
        super().__init__(
            name="route-openstreetmap",
            url=OSRM_ROUTING_BASE_URL,
        )

    def _fetch_once(self, params: Dict[str, Any]) -> Dict[str, Any]:
        city = params.get("city")
        origin = _location_query(str(params["origin"]), city)
        destination = _location_query(str(params["destination"]), city)
        mode = str(params.get("mode") or "driving")
        if mode not in self._PROFILES:
            raise ValueError("公共 OpenStreetMap 路线仅支持 driving 和 walking")
        profile, osrm_mode = self._PROFILES[mode]
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="route-geocode") as executor:
            start_future = executor.submit(_geocode, origin)
            end_future = executor.submit(_geocode, destination)
            start = start_future.result()
            end = end_future.result()
        coordinates = (
            f"{start['longitude']:.6f},{start['latitude']:.6f};"
            f"{end['longitude']:.6f},{end['latitude']:.6f}"
        )
        route_error: Optional[Exception] = None
        payload: Dict[str, Any] = {}
        for base_url in (OSRM_ROUTING_BASE_URL, OSRM_PUBLIC_FALLBACK_URL):
            try:
                if base_url == OSRM_ROUTING_BASE_URL:
                    route_url = (
                        f"{base_url}/{profile}/route/v1/{osrm_mode}/{coordinates}"
                    )
                else:
                    route_url = f"{base_url}/route/v1/{osrm_mode}/{coordinates}"
                payload = _request_json(
                    route_url,
                    {"overview": "false", "steps": "false"},
                    timeout=15.0,
                    attempts=2,
                )
                route_error = None
                break
            except Exception as exc:
                route_error = exc
        if route_error is not None:
            raise route_error
        routes = payload.get("routes") or []
        if not routes:
            raise ValueError("OpenStreetMap 未返回可用路线")
        route = routes[0]
        distance_km = float(route["distance"]) / 1000
        duration_min = float(route["duration"]) / 60
        mode_names = {"driving": "驾车", "walking": "步行"}
        return {
            "origin": start["name"],
            "destination": end["name"],
            "mode": mode_names[mode],
            "distance": f"{distance_km:.1f}公里",
            "duration": f"{duration_min:.0f}分钟",
            "route": f"从{start['name']}到{end['name']}的{mode_names[mode]}路线",
            "distance_km": distance_km,
            "duration_minutes": duration_min,
            "start": start,
            "end": end,
        }


def get_weather_provider() -> JsonHttpProvider:
    custom_url = os.environ.get("SMARTLIFE_WEATHER_API_URL", "")
    if custom_url:
        return JsonHttpProvider(
            name="weather",
            url=custom_url,
            api_key=os.environ.get("SMARTLIFE_WEATHER_API_KEY", ""),
        )
    return OpenMeteoWeatherProvider()


def get_route_provider() -> JsonHttpProvider:
    custom_url = os.environ.get("SMARTLIFE_ROUTE_API_URL", "")
    if custom_url:
        return JsonHttpProvider(
            name="route",
            url=custom_url,
            api_key=os.environ.get("SMARTLIFE_ROUTE_API_KEY", ""),
        )
    return OpenStreetMapRouteProvider()


def get_provider_status() -> Dict[str, bool]:
    return {
        "weather": get_weather_provider().configured,
        "route": get_route_provider().configured,
    }
