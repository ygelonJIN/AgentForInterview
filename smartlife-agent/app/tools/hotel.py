"""酒店搜索工具。"""
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.observability import log_warning
from app.tools.providers import get_hotel_provider


class HotelSearchInput(BaseModel):
    city: str = Field(description="城市名称")
    check_in: str = Field(description="入住日期，格式 YYYY-MM-DD")
    check_out: str = Field(description="退房日期，格式 YYYY-MM-DD")
    budget_per_night: Optional[float] = Field(default=None, description="每晚预算")
    hotel_type: Optional[str] = Field(default=None, description="酒店类型")


@tool("search_hotels", args_schema=HotelSearchInput)
def search_hotels(
    city: str,
    check_in: str,
    check_out: str,
    budget_per_night: Optional[float] = None,
    hotel_type: Optional[str] = None,
) -> dict:
    """搜索酒店；未配置外部 provider 时返回明确标记的本地模拟结果。"""
    provider = get_hotel_provider()
    if provider.configured:
        try:
            external = provider.fetch({
                "city": city,
                "check_in": check_in,
                "check_out": check_out,
                "budget_per_night": budget_per_night,
                "hotel_type": hotel_type,
            })
            hotels = external.get("hotels", external.get("results", []))
            return {
                "hotels": hotels,
                "count": len(hotels) if isinstance(hotels, list) else 0,
                "simulated": False,
                "source": provider.url,
            }
        except Exception as exc:
            log_warning("tools.hotel_provider", str(exc), {"city": city})
    else:
        log_warning("tools.hotel_provider", "未配置外部酒店 provider，使用本地模拟", {"city": city})

    hotels = [{
        "id": "hotel_mock_001",
        "name": f"{city}示范酒店",
        "city": city,
        "price_per_night": budget_per_night or 400,
        "hotel_type": hotel_type or "舒适",
        "rating": 4.5,
    }]
    return {
        "hotels": hotels,
        "count": len(hotels),
        "simulated": True,
        "source": "local_mock_hotel",
    }


def get_hotel_tools():
    return [search_hotels]
