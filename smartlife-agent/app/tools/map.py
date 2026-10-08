"""真实路线查询工具。"""

from typing import Literal, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field, model_validator

from app.tools.providers import ProviderUnavailable, get_route_provider


class MapInput(BaseModel):
    origin: str = Field(min_length=1, max_length=120, description="起点")
    destination: str = Field(min_length=1, max_length=120, description="终点")
    mode: Literal["driving", "walking"] = Field(
        default="driving",
        description="交通方式：driving/walking",
    )
    city: Optional[str] = Field(
        default=None,
        max_length=80,
        description="城市或区域上下文，用于消歧同名地点",
    )

    @model_validator(mode="after")
    def validate_locations(self):
        if self.origin.strip() == self.destination.strip():
            raise ValueError("origin 和 destination 不能相同")
        return self


@tool("get_route", args_schema=MapInput)
def get_route(
    origin: str,
    destination: str,
    mode: str = "driving",
    city: Optional[str] = None,
) -> dict:
    """获取两点之间的真实驾车或步行路线、距离、预计时间和来源信息。"""
    provider = get_route_provider()
    try:
        provider_response = provider.fetch_with_meta({
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "city": city,
        })
        external = provider_response.payload
        if not any(key in external for key in ("distance", "duration", "route")):
            raise ValueError("route provider 缺少路线结果字段")
        return {
            "origin": external.get("origin", origin),
            "destination": external.get("destination", destination),
            "mode": external.get("mode", mode),
            "city": city,
            "distance": external.get("distance", ""),
            "duration": external.get("duration", ""),
            "route": external.get("route", ""),
            "distance_km": external.get("distance_km"),
            "duration_minutes": external.get("duration_minutes"),
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
        raise ProviderUnavailable(f"路线 provider 不可用: {exc}") from exc


def get_map_tools():
    return [get_route]
