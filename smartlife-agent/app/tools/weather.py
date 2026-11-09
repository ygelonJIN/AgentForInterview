"""真实天气查询工具。"""

from datetime import date as date_type

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from app.tools.providers import (
    ProviderUnavailable,
    get_air_quality_provider,
    get_weather_provider,
)


class WeatherInput(BaseModel):
    city: str = Field(min_length=1, max_length=80, description="城市名称")
    date: str = Field(description="日期，格式 YYYY-MM-DD")

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        try:
            parsed = date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是有效的 YYYY-MM-DD 日期") from exc
        if parsed.isoformat() != value:
            raise ValueError("date 必须使用 YYYY-MM-DD 格式")
        return value


@tool("get_weather", args_schema=WeatherInput)
def get_weather(city: str, date: str) -> dict:
    """获取指定城市和日期的真实天气，包括温度、天气状况、湿度和来源信息。"""
    provider = get_weather_provider()
    try:
        provider_response = provider.fetch_with_meta({"city": city, "date": date})
        external = provider_response.payload
        if not any(key in external for key in ("temperature", "condition", "humidity")):
            raise ValueError("weather provider 缺少天气结果字段")
        return {
            "city": external.get("city", city),
            "date": external.get("date", date),
            "temperature": external.get("temperature", ""),
            "temperature_min": external.get("temperature_min"),
            "temperature_max": external.get("temperature_max"),
            "condition": external.get("condition", ""),
            "humidity": external.get("humidity", ""),
            "precipitation_probability": external.get("precipitation_probability"),
            "sunrise": external.get("sunrise", ""),
            "sunset": external.get("sunset", ""),
            "uv_index_max": external.get("uv_index_max"),
            "suggestion": external.get("suggestion", ""),
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
        raise ProviderUnavailable(f"天气 provider 不可用: {exc}") from exc


class WeatherForecastInput(BaseModel):
    city: str = Field(min_length=1, max_length=80, description="城市名称")
    start_date: str = Field(description="第一天，格式 YYYY-MM-DD")
    days: int = Field(default=3, ge=1, le=16, description="连续天数，最多 16 天")

    @field_validator("start_date")
    @classmethod
    def validate_start_date(cls, value: str) -> str:
        try:
            parsed = date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("start_date 必须是有效的 YYYY-MM-DD 日期") from exc
        if parsed.isoformat() != value:
            raise ValueError("start_date 必须使用 YYYY-MM-DD 格式")
        return value


@tool("get_weather_forecast", args_schema=WeatherForecastInput)
def get_weather_forecast(city: str, start_date: str, days: int = 3) -> dict:
    """一次获取连续多天的逐日天气：行程有几天就查几天，不要逐天分次调用。"""
    provider = get_weather_provider()
    try:
        provider_response = provider.fetch_with_meta(
            {"city": city, "date": start_date, "days": int(days)}
        )
        external = provider_response.payload
        rows = external.get("days")
        if not isinstance(rows, list) or not rows:
            raise ValueError("weather provider 未返回逐日预报")
        return {
            "city": external.get("city", city),
            "start": external.get("start", start_date),
            "days": rows,
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
        raise ProviderUnavailable(f"天气 provider 不可用: {exc}") from exc



class AirQualityInput(BaseModel):
    city: str = Field(min_length=1, max_length=80, description="城市名称")
    start_date: str = Field(description="第一天，格式 YYYY-MM-DD")
    days: int = Field(default=1, ge=1, le=5, description="连续天数，最多 5 天")

    @field_validator("start_date")
    @classmethod
    def validate_start_date(cls, value: str) -> str:
        try:
            parsed = date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("start_date 必须是有效的 YYYY-MM-DD 日期") from exc
        if parsed.isoformat() != value:
            raise ValueError("start_date 必须使用 YYYY-MM-DD 格式")
        return value


@tool("get_air_quality", args_schema=AirQualityInput)
def get_air_quality(city: str, start_date: str, days: int = 1) -> dict:
    """获取逐日空气质量（PM2.5 与 US AQI 的当日最大值），用于判断能否长时间户外。"""
    provider = get_air_quality_provider()
    try:
        provider_response = provider.fetch_with_meta(
            {"city": city, "date": start_date, "days": int(days)}
        )
        external = provider_response.payload
        rows = external.get("days")
        if not isinstance(rows, list) or not rows:
            raise ValueError("空气质量 provider 未返回逐日结果")
        return {
            "city": external.get("city", city),
            "start": external.get("start", start_date),
            "days": rows,
            "standard": external.get("standard", "us_aqi"),
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
        raise ProviderUnavailable(f"空气质量 provider 不可用: {exc}") from exc
