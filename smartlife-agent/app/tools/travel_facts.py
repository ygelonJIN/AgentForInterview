"""旅行相关的实时外部事实：汇率与法定节假日。

和天气、路线一样，这类数据只做"查回来是什么就说什么"，不推断、不补全：
汇率按自然日更新，节假日只覆盖公共假日（不含调休上班日）。
"""

from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.tools.providers import (
    ProviderUnavailable,
    get_fx_provider,
    get_holiday_provider,
)

# 目的地 -> 当地货币。只覆盖词表里出现过的境外目的地；表外一律不猜。
_CURRENCY_GROUPS = {
    ("JPY", "日元"): ("日本", "东京", "大阪", "京都", "北海道", "冲绳"),
    ("KRW", "韩元"): ("韩国", "首尔", "济州岛"),
    ("THB", "泰铢"): ("泰国", "曼谷", "清迈", "普吉", "普吉岛", "芭提雅"),
    ("SGD", "新加坡元"): ("新加坡",),
    ("MYR", "林吉特"): ("马来西亚", "吉隆坡", "沙巴"),
    ("VND", "越南盾"): ("越南", "岘港", "芽庄"),
    ("IDR", "印尼盾"): ("巴厘岛", "印度尼西亚", "印尼"),
    ("AED", "迪拉姆"): ("迪拜", "阿联酋"),
    ("TRY", "里拉"): ("土耳其",),
    ("EGP", "埃及镑"): ("埃及",),
    ("EUR", "欧元"): ("法国", "巴黎", "意大利", "罗马", "西班牙", "德国", "欧洲"),
    ("GBP", "英镑"): ("英国", "伦敦"),
    ("CHF", "瑞士法郎"): ("瑞士",),
    ("ISK", "冰岛克朗"): ("冰岛",),
    ("USD", "美元"): ("美国", "纽约"),
    ("AUD", "澳元"): ("澳大利亚",),
    ("NZD", "新西兰元"): ("新西兰",),
    ("CAD", "加元"): ("加拿大",),
    ("MVR", "拉菲亚"): ("马尔代夫",),
    ("HKD", "港币"): ("香港",),
    ("MOP", "澳门元"): ("澳门",),
    ("TWD", "新台币"): ("台湾", "台北"),
}

CURRENCY_BY_PLACE = {
    place: code_name for code_name, places in _CURRENCY_GROUPS.items() for place in places
}


def currency_for(destination: str) -> Optional[tuple[str, str]]:
    """目的地 -> (货币代码, 中文名)。境外表外返回 None，不猜。"""
    place = str(destination or "").strip()
    if not place:
        return None
    if place in CURRENCY_BY_PLACE:
        return CURRENCY_BY_PLACE[place]
    for key in sorted(CURRENCY_BY_PLACE, key=len, reverse=True):
        if place.startswith(key) or key in place:
            return CURRENCY_BY_PLACE[key]
    return None


class ExchangeRateInput(BaseModel):
    base: str = Field(default="CNY", min_length=3, max_length=3, description="本币，ISO 4217")
    quote: str = Field(min_length=3, max_length=3, description="目标货币，ISO 4217")


@tool("get_exchange_rate", args_schema=ExchangeRateInput)
def get_exchange_rate(base: str = "CNY", quote: str = "JPY") -> dict:
    """查询参考汇率（欧洲央行，按自然日更新）：1 本币折合多少目标货币。"""
    provider = get_fx_provider()
    try:
        provider_response = provider.fetch_with_meta(
            {"base": str(base).upper(), "quote": str(quote).upper()}
        )
        external = provider_response.payload
        rate = external.get("rate")
        if rate is None:
            raise ValueError("汇率 provider 缺少 rate 字段")
        return {
            "base": external.get("base", base).upper(),
            "quote": external.get("quote", quote).upper(),
            "rate": float(rate),
            "date": external.get("date", ""),
            "simulated": False,
            "source": provider.url,
            "cache_status": provider_response.cache_status,
            "fetched_at": provider_response.fetched_at,
            "stale": provider_response.stale,
            "warnings": list(provider_response.warnings),
        }
    except Exception as exc:
        if isinstance(exc, ProviderUnavailable):
            raise
        raise ProviderUnavailable(f"汇率 provider 不可用: {exc}") from exc


class HolidayInput(BaseModel):
    country: str = Field(default="CN", min_length=2, max_length=2, description="ISO 3166-1 两位国家码")
    year: int = Field(ge=2020, le=2100, description="年份")


@tool("get_public_holidays", args_schema=HolidayInput)
def get_public_holidays(country: str = "CN", year: int = 2026) -> dict:
    """查询某国某年的法定公共假日（不含调休上班日），用于判断行程是否落在假期。"""
    provider = get_holiday_provider()
    try:
        provider_response = provider.fetch_with_meta(
            {"country": str(country).upper(), "year": int(year)}
        )
        external = provider_response.payload
        holidays = external.get("holidays")
        if not isinstance(holidays, list):
            raise ValueError("假日 provider 缺少 holidays 字段")
        return {
            "country": external.get("country", country).upper(),
            "year": external.get("year", year),
            "holidays": holidays,
            "simulated": False,
            "source": provider.url,
            "cache_status": provider_response.cache_status,
            "fetched_at": provider_response.fetched_at,
            "stale": provider_response.stale,
            "warnings": list(provider_response.warnings),
        }
    except Exception as exc:
        if isinstance(exc, ProviderUnavailable):
            raise
        raise ProviderUnavailable(f"假日 provider 不可用: {exc}") from exc


def holidays_in_range(holidays, dates) -> list[dict]:
    """过滤出落在给定日期集合里的假日；日期都是 YYYY-MM-DD。"""
    wanted = {str(item) for item in dates or []}
    return [
        item
        for item in holidays or []
        if isinstance(item, dict) and str(item.get("date") or "") in wanted
    ]
