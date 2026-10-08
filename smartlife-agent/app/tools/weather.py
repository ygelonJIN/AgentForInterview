"""真实天气查询工具。"""

from datetime import date as date_type

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from app.tools.providers import ProviderUnavailable, get_weather_provider


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


def get_weather_tools():
    return [get_weather]
