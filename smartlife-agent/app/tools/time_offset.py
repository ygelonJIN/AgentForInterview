"""时区与时差工具：离线、无 Key。

为什么不用「经度 ÷ 15」粗算：中国全境一个时区、印度 +5:30、西班牙用中欧时间，
粗算在这三处都会错。这里用 IANA 时区库（``zoneinfo``）按城市解析。

城市 → 时区走两级：
1. ``CITY_ZONES``：多时区国家（美/加/澳/印尼）和容易歧义的城市，精确到城；
2. ``COUNTRY_ZONES``：单时区国家按国家码取主时区，``precision="country"``。

查不到就返回 ``available=False``，不外推、不猜。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from app.tools import geo

# 多时区国家 / 特殊城市：精确到城市。
CITY_ZONES: Dict[str, str] = {
    "巴厘岛": "Asia/Makassar",
    "登巴萨": "Asia/Makassar",
    "纽约": "America/New_York",
    "洛杉矶": "America/Los_Angeles",
    "旧金山": "America/Los_Angeles",
    "西雅图": "America/Los_Angeles",
    "芝加哥": "America/Chicago",
    "温哥华": "America/Vancouver",
    "多伦多": "America/Toronto",
    "悉尼": "Australia/Sydney",
    "墨尔本": "Australia/Melbourne",
    "珀斯": "Australia/Perth",
    "奥克兰": "Pacific/Auckland",
    "圣托里尼": "Europe/Athens",
    "雷克雅未克": "Atlantic/Reykjavik",
    "马拉喀什": "Africa/Casablanca",
    "伊斯坦布尔": "Europe/Istanbul",
    "迪拜": "Asia/Dubai",
    "莫斯科": "Europe/Moscow",
    "新德里": "Asia/Kolkata",
    "孟买": "Asia/Kolkata",
}

# 单时区国家：ISO2 → 主时区。
COUNTRY_ZONES: Dict[str, str] = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "MO": "Asia/Macau",
    "TW": "Asia/Taipei",
    "SG": "Asia/Singapore",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "TH": "Asia/Bangkok",
    "VN": "Asia/Ho_Chi_Minh",
    "MY": "Asia/Kuala_Lumpur",
    "PH": "Asia/Manila",
    "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh",
    "TR": "Europe/Istanbul",
    "IN": "Asia/Kolkata",
    "LK": "Asia/Colombo",
    "NP": "Asia/Kathmandu",
    "ID": "Asia/Jakarta",
    "FR": "Europe/Paris",
    "GB": "Europe/London",
    "IE": "Europe/Dublin",
    "DE": "Europe/Berlin",
    "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels",
    "AT": "Europe/Vienna",
    "CH": "Europe/Zurich",
    "IT": "Europe/Rome",
    "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon",
    "GR": "Europe/Athens",
    "IS": "Atlantic/Reykjavik",
    "NO": "Europe/Oslo",
    "SE": "Europe/Stockholm",
    "DK": "Europe/Copenhagen",
    "FI": "Europe/Helsinki",
    "PL": "Europe/Warsaw",
    "CZ": "Europe/Prague",
    "HU": "Europe/Budapest",
    "RU": "Europe/Moscow",
    "US": "America/New_York",
    "CA": "America/Toronto",
    "MX": "America/Mexico_City",
    "BR": "America/Sao_Paulo",
    "AR": "America/Argentina/Buenos_Aires",
    "PE": "America/Lima",
    "CL": "America/Santiago",
    "CO": "America/Bogota",
    "AU": "Australia/Sydney",
    "NZ": "Pacific/Auckland",
    "ZA": "Africa/Johannesburg",
    "EG": "Africa/Cairo",
    "MA": "Africa/Casablanca",
    "KE": "Africa/Nairobi",
    "ET": "Africa/Addis_Ababa",
    "NG": "Africa/Lagos",
}

BEIJING_OFFSET = 8.0


def resolve_zone(city: str) -> Tuple[Optional[str], str]:
    """城市 → (IANA 时区, 精度)。精度为 city / country / unresolved。"""
    alias = geo.lookup_alias(city)
    if alias is None:
        return None, "unresolved"
    canonical, _english, country_code = alias
    if canonical in CITY_ZONES:
        return CITY_ZONES[canonical], "city"
    zone = COUNTRY_ZONES.get(str(country_code or "").upper())
    return zone, "country" if zone else "unresolved"


def offset_hours(zone: str, on: Optional[date] = None) -> float:
    """该时区在指定日期的 UTC 偏移（含夏令时）。取当天正午，避开边界。"""
    day = on or date.today()
    local_noon = datetime.combine(day, time(12), tzinfo=ZoneInfo(zone))
    delta = local_noon.utcoffset() or timedelta(0)
    return round(delta.total_seconds() / 3600, 2)


def describe_offset(hours: float) -> str:
    sign = "+" if hours >= 0 else "-"
    value = abs(hours)
    whole = int(value)
    minutes = int(round((value - whole) * 60))
    return f"UTC{sign}{whole:02d}:{minutes:02d}"


def time_difference(city: str, on: Optional[date] = None) -> Dict[str, Any]:
    """时差的纯计算版本，给 trip_gear 的 derive 直接调用（不经过工具注册表）。"""
    zone, precision = resolve_zone(city)
    if not zone:
        return {
            "available": False,
            "city": city,
            "reason": "没有该城市的时区数据，未写入时差（不按经度粗算）",
            "source": "zoneinfo",
        }
    hours = offset_hours(zone, on)
    diff = round(hours - BEIJING_OFFSET, 2)
    return {
        "available": True,
        "city": city,
        "timezone": zone,
        "utc_offset": hours,
        "utc_offset_text": describe_offset(hours),
        "beijing_diff_hours": diff,
        "precision": precision,
        "date": (on or date.today()).isoformat(),
        "source": "zoneinfo",
        # 精度是 national 时，同一国家的多时区城市可能差 1 小时以上，要如实标。
        "note": (
            "按国家主时区推算，多时区国家的其它城市可能不同"
            if precision == "country"
            else ""
        ),
    }


class TimeOffsetInput(BaseModel):
    city: str = Field(min_length=1, description="城市名称，如：东京")
    date: Optional[str] = Field(default=None, description="日期，格式 YYYY-MM-DD；默认今天")

    @field_validator("date")
    @classmethod
    def _validate_date(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须使用 YYYY-MM-DD 格式") from exc
        if parsed.isoformat() != value:
            raise ValueError("date 必须使用 YYYY-MM-DD 格式")
        return value


@tool("get_time_offset", args_schema=TimeOffsetInput)
def get_time_offset(city: str, date: Optional[str] = None) -> dict:
    """查询目的地与北京时间的时差、UTC 偏移和当地时区（离线计算，无 Key）。"""
    target = datetime.fromisoformat(date).date() if date else None
    payload = time_difference(city, target)
    if not payload.get("available"):
        return payload
    now_local = datetime.now(ZoneInfo(payload["timezone"]))
    payload["local_time"] = now_local.strftime("%Y-%m-%d %H:%M")
    return payload
