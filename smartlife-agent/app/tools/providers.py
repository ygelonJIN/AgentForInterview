"""真实外部工具 provider 配置、HTTP 适配、缓存和熔断。

天气默认使用 Open-Meteo，路线默认使用 OpenStreetMap 公共路线服务。
这些默认服务免费且无需 API Key，但属于公共公平使用服务，不提供 SLA。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

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


def _at(values: Any, index: int) -> Any:
    """按索引取数组元素；缺字段或长度不够都返回 None，不抛异常。"""
    if isinstance(values, list) and 0 <= index < len(values):
        return values[index]
    return None


def _epoch_from_monotonic(created_at: float) -> float:
    return time.time() - max(0.0, time.monotonic() - created_at)


def _cache_ttl(name: str) -> float:
    if name.startswith("activities"):
        default = "3600"
    else:
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
    from app.tools import geo

    _CIRCUIT_BREAKERS.clear()
    _RESPONSE_CACHES.clear()
    geo.reset_cache()


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
OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
FRANKFURTER_URL = "https://api.frankfurter.app/latest"
NAGER_HOLIDAY_URL = "https://date.nager.at/api/v3/PublicHolidays"
OSRM_ROUTING_BASE_URL = "https://routing.openstreetmap.de"
OSRM_PUBLIC_FALLBACK_URL = "https://router.project-osrm.org"
PHOTON_SEARCH_URL = "https://photon.komoot.io/api/"


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


def _geocode(query: str) -> Dict[str, Any]:
    """地名 → 坐标；实现搬到 ``app.tools.geo``（别名表 + 择优 + 保守拒答）。"""
    from app.tools import geo

    return geo.resolve_location(query).as_dict()


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

    MAX_FORECAST_DAYS = 16

    def _fetch_once(self, params: Dict[str, Any]) -> Dict[str, Any]:
        city = str(params["city"]).strip()
        start = date.fromisoformat(str(params["date"]))
        today = date.today()
        try:
            span = int(params.get("days") or 1)
        except (TypeError, ValueError):
            span = 1
        span = max(1, min(span, self.MAX_FORECAST_DAYS))
        end = start + timedelta(days=span - 1)
        if start < today or end > today + timedelta(days=15):
            raise ValueError("Open-Meteo 预报仅支持今天至未来 15 天")

        location = _geocode(city)
        forecast = _request_json(
            OPEN_METEO_FORECAST_URL,
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "hourly": "relative_humidity_2m",
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,sunrise,sunset,uv_index_max"
                ),
                "forecast_days": 16,
                "timezone": "auto",
            },
        )
        rows = [
            self._daily_row(location, forecast, start + timedelta(days=offset), today)
            for offset in range(span)
        ]
        if span == 1:
            return rows[0]
        # 一次请求就拿整段：逐日排程只需要一次 HTTP，不必按天各发一次。
        return {
            "city": location["name"],
            "start": start.isoformat(),
            "days": rows,
            "latitude": location["latitude"],
            "longitude": location["longitude"],
        }

    def _daily_row(
        self,
        location: Dict[str, Any],
        forecast: Dict[str, Any],
        target: date,
        today: date,
    ) -> Dict[str, Any]:
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
            # 日出日落与紫外线：查一次天气顺带就有，用来判"这天几点天黑""要不要防晒"
            "sunrise": _at(daily.get("sunrise"), index),
            "sunset": _at(daily.get("sunset"), index),
            "uv_index_max": _at(daily.get("uv_index_max"), index),
            "suggestion": suggestion,
            "latitude": location["latitude"],
            "longitude": location["longitude"],
        }


class OpenMeteoAirQualityProvider(JsonHttpProvider):
    """Open-Meteo 空气质量：免 key，按小时取，聚合成每天的最大值。"""

    MAX_FORECAST_DAYS = 5

    def __init__(self):
        super().__init__(name="air-quality-open-meteo", url=OPEN_METEO_AIR_QUALITY_URL)

    def _fetch_once(self, params: Dict[str, Any]) -> Dict[str, Any]:
        city = str(params["city"]).strip()
        start = date.fromisoformat(str(params["date"]))
        try:
            span = int(params.get("days") or 1)
        except (TypeError, ValueError):
            span = 1
        span = max(1, min(span, self.MAX_FORECAST_DAYS))
        today = date.today()
        if start < today or start + timedelta(days=span - 1) > today + timedelta(days=self.MAX_FORECAST_DAYS - 1):
            raise ValueError(f"空气质量预报仅支持今天至未来 {self.MAX_FORECAST_DAYS} 天")

        location = _geocode(city)
        payload = _request_json(
            OPEN_METEO_AIR_QUALITY_URL,
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "hourly": "pm2_5,us_aqi",
                "forecast_days": self.MAX_FORECAST_DAYS,
                "timezone": "auto",
            },
        )
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        rows = []
        for offset in range(span):
            target = (start + timedelta(days=offset)).isoformat()
            pm25 = [
                float(value)
                for timestamp, value in zip(times, hourly.get("pm2_5") or [])
                if str(timestamp).startswith(target) and value is not None
            ]
            aqi = [
                float(value)
                for timestamp, value in zip(times, hourly.get("us_aqi") or [])
                if str(timestamp).startswith(target) and value is not None
            ]
            rows.append({
                "date": target,
                "pm2_5_max": round(max(pm25), 1) if pm25 else None,
                "aqi_max": round(max(aqi), 1) if aqi else None,
            })
        if not any(row["aqi_max"] is not None for row in rows):
            raise ValueError("Open-Meteo 未返回空气质量数据")
        return {
            "city": location["name"],
            "start": start.isoformat(),
            "days": rows,
            "standard": "us_aqi",
        }


class FrankfurterFxProvider(JsonHttpProvider):
    """欧洲央行参考汇率（Frankfurter）：免 key，按自然日更新。"""

    def __init__(self):
        super().__init__(name="fx-frankfurter", url=FRANKFURTER_URL)

    def _fetch_once(self, params: Dict[str, Any]) -> Dict[str, Any]:
        base = str(params["base"]).strip().upper()
        quote = str(params["quote"]).strip().upper()
        payload = _request_json(FRANKFURTER_URL, {"from": base, "to": quote})
        rates = payload.get("rates") or {}
        if quote not in rates:
            raise ValueError(f"汇率 provider 未返回 {base}->{quote}")
        return {
            "base": base,
            "quote": quote,
            "rate": float(rates[quote]),
            "date": str(payload.get("date") or ""),
        }


class NagerHolidayProvider(JsonHttpProvider):
    """Nager.Date 公共假日：免 key，按国家 + 年份查。"""

    def __init__(self):
        super().__init__(name="holidays-nager", url=NAGER_HOLIDAY_URL)

    def _fetch_once(self, params: Dict[str, Any]) -> Dict[str, Any]:
        country = str(params["country"]).strip().upper()
        year = int(params["year"])
        payload = _request_json(f"{NAGER_HOLIDAY_URL}/{year}/{country}", {})
        items = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ValueError("假日 provider 返回格式异常")
        return {
            "country": country,
            "year": year,
            "holidays": [
                {
                    "date": str(item.get("date") or ""),
                    "name": str(item.get("name") or ""),
                    "local_name": str(item.get("localName") or ""),
                }
                for item in items
                if isinstance(item, dict) and item.get("date")
            ],
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


# Photon 结果里值得当成"可去的地方"的 OSM 标签
_PLACE_KINDS = {
    ("tourism", "attraction"): "景点",
    ("tourism", "museum"): "博物馆",
    ("tourism", "gallery"): "美术馆",
    ("tourism", "zoo"): "动物园",
    ("tourism", "theme_park"): "主题乐园",
    ("tourism", "viewpoint"): "观景点",
    ("tourism", "artwork"): "公共艺术",
    ("leisure", "park"): "公园",
    ("leisure", "garden"): "花园",
    ("leisure", "nature_reserve"): "自然保护区",
    ("amenity", "marketplace"): "市集",
    ("amenity", "theatre"): "剧院",
    ("amenity", "arts_centre"): "艺术中心",
}


# 目标用户读不了、且基本属于标注错误的文字：西里尔字母只在俄语系国家才是本地名
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")
_CYRILLIC_COUNTRIES = {"RU", "BY", "UA", "BG", "RS", "MK", "KZ", "KG", "TJ", "MN"}
# OSM 的 tourism=attraction 很宽松，工厂和贸易公司也会被标成"景点"
_COMPANY_MARKERS = (
    " co.", " co,", " ltd", " inc.", " inc(", " llc", " company", " corporation",
    " gmbh", " pte", " sdn", " factory", " manufactur", " printing machinery",
)


def _is_noisy_place(name: str, country_code: str) -> bool:
    """过滤 OSM 里的噪声 POI：公司工厂、读不了的文字、空名与单字名。"""
    text = str(name or "").strip()
    if len(text) <= 1:
        return True
    lowered = f" {text.lower()} "
    if any(marker in lowered for marker in _COMPANY_MARKERS):
        return True
    if _CYRILLIC.search(text) and str(country_code or "").upper() not in _CYRILLIC_COUNTRIES:
        return True
    return False


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点球面距离（km），用于把 Photon 的全局召回裁到城市附近。"""
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, a ** 0.5))


class PhotonPlacesProvider(JsonHttpProvider):
    """OSM/Photon 免 key 场所 provider。

    覆盖全球，给的是"这个城市有什么可去的地方"（无场次、无票价），
    属于计划性证据而不是票务事实——所以结果里不编时间，也不进预算。

    两个必须做的裁剪，否则会答出别的国家的同名地点：
    ``lat``/``lon`` 做地理偏置，再按到城市中心的距离过滤。
    """

    # Photon 的 q 只做名称匹配，`q=park` 会返回叫 "Park" 的酒店和商场。
    # 按类别取景点必须用 osm_tag 过滤，q 只负责把范围收敛到这座城市。
    # 多个 osm_tag 之间是 OR。分组查询是为了让"可参与"的冷门类别不被景点挤掉：
    # 三类混查时 limit 配额会被 attraction 占满，theatre 一条都剩不下。
    _TAG_GROUPS = (
        ("tourism:attraction", "tourism:museum", "tourism:viewpoint"),
        ("leisure:park", "amenity:marketplace"),
        ("amenity:theatre", "amenity:arts_centre"),
    )
    # 这些是"可以买票 / 报名参加"的场所，其余只是"可去之处"
    _PARTICIPATORY = {"theatre", "arts_centre", "community_centre"}
    _MAX_DISTANCE_KM = 60.0

    def __init__(self):
        super().__init__(name="activities-photon", url=PHOTON_SEARCH_URL)

    def _cache_key(self, params: Dict[str, Any]) -> str:
        """场所清单与日期无关：把 date 从缓存键里剔掉。

        否则同一城市 5 天的行程会把同一份清单请求 5 遍（每天一次）。
        """
        return super()._cache_key({
            key: value for key, value in params.items() if key != "date"
        })

    def _center(self, params: Dict[str, Any]) -> Tuple[str, Optional[float], Optional[float]]:
        city = str(params.get("city") or "").strip()
        english = str(params.get("english") or "").strip()
        latitude = params.get("latitude")
        longitude = params.get("longitude")
        if latitude is not None and longitude is not None and english:
            return english, float(latitude), float(longitude)
        from app.tools import geo

        alias = geo.lookup_alias(city)
        if alias is not None:
            english = english or alias[1]
        location = geo.resolve_location(city)
        return english or location.english, location.latitude, location.longitude

    def _fetch_once(self, params: Dict[str, Any]) -> Dict[str, Any]:
        city = str(params.get("city") or "").strip()
        if not city and not str(params.get("english") or "").strip():
            raise ValueError("缺少城市")
        country_code = str(params.get("country_code") or "").strip()
        english, latitude, longitude = self._center(params)

        items: List[Dict[str, Any]] = []
        seen: set = set()
        for tag_group in self._TAG_GROUPS:
            query: Dict[str, Any] = {"q": english, "osm_tag": list(tag_group), "limit": 8}
            if latitude is not None and longitude is not None:
                query.update({"lat": latitude, "lon": longitude})
            try:
                payload = _request_json(PHOTON_SEARCH_URL, query, timeout=8.0, attempts=2)
            except Exception:
                continue
            for feature in payload.get("features") or []:
                props = feature.get("properties") or {}
                kind = _PLACE_KINDS.get((str(props.get("osm_key") or ""), str(props.get("osm_value") or "")))
                name = str(props.get("name") or "").strip()
                if not kind or not name:
                    continue
                if _is_noisy_place(name, country_code):
                    continue
                coordinates = (feature.get("geometry") or {}).get("coordinates") or []
                item_lat = coordinates[1] if len(coordinates) == 2 else None
                item_lon = coordinates[0] if len(coordinates) == 2 else None
                if None not in (latitude, longitude, item_lat, item_lon):
                    if _distance_km(float(latitude), float(longitude), float(item_lat), float(item_lon)) > self._MAX_DISTANCE_KM:
                        continue
                dedupe = (name.casefold(), kind)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                items.append({
                    "title": name,
                    "kind": kind,
                    "participatory": str(props.get("osm_value") or "") in self._PARTICIPATORY,
                    "city": str(props.get("city") or "") or city,
                    "country": str(props.get("country") or ""),
                    "address": " ".join(
                        part for part in (
                            str(props.get("street") or "").strip(),
                            str(props.get("housenumber") or "").strip(),
                        ) if part
                    ),
                    "latitude": item_lat,
                    "longitude": item_lon,
                    "start": "",
                    "price_min": None,
                    "price_max": None,
                    "currency": "",
                    "url": "",
                })
        if not items:
            raise ValueError("Photon 未返回可用场所")
        # 可参与的排前面：截断时不能被景点挤掉，否则"想参加活动"又落空
        items.sort(key=lambda item: (not item.get("participatory"),))
        return {
            "city": city,
            "items": items[:12],
            "source": "photon-osm",
            "kind": "poi",
            "note": "剧院与艺术中心可到官方渠道查档期买票；景点与公园是常设开放场所",
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


def get_air_quality_provider() -> JsonHttpProvider:
    return OpenMeteoAirQualityProvider()


def get_fx_provider() -> JsonHttpProvider:
    return FrankfurterFxProvider()


def get_holiday_provider() -> JsonHttpProvider:
    return NagerHolidayProvider()


def get_route_provider() -> JsonHttpProvider:
    custom_url = os.environ.get("SMARTLIFE_ROUTE_API_URL", "")
    if custom_url:
        return JsonHttpProvider(
            name="route",
            url=custom_url,
            api_key=os.environ.get("SMARTLIFE_ROUTE_API_KEY", ""),
        )
    return OpenStreetMapRouteProvider()


def get_activities_provider() -> JsonHttpProvider:
    """活动 provider：只用免 key 的 OpenStreetMap 场所数据。

    场次级数据（某天演什么、票价多少）没有可用的公开接口：
    Ticketmaster 需要 Key 且没有中国业务，秀动/大麦要签名或反爬，场馆官网是前端渲染。
    所以这里不做场次。用户确实要参加某个活动时，由用户在需求里指定，
    走 ``TripFacts`` 的固定条目，不依赖任何 provider。
    """
    return PhotonPlacesProvider()


def get_provider_status() -> Dict[str, Any]:
    try:
        activities = get_activities_provider()
        activities_name, activities_ready = activities.name, activities.configured
    except Exception as exc:  # provider 未配置不应影响整体状态查询
        activities_name, activities_ready = f"unavailable: {type(exc).__name__}", False
    return {
        "weather": get_weather_provider().configured,
        "air_quality": get_air_quality_provider().configured,
        "fx": get_fx_provider().configured,
        "holidays": get_holiday_provider().configured,
        "route": get_route_provider().configured,
        "activities": activities_ready,
        "activities_provider": activities_name,
    }
