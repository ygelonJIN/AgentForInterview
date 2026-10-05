from .weather import get_weather, get_weather_tools
from .map import get_route, get_map_tools
from .payment import get_payment_tools
from .time import get_current_time, get_time_tools
from .hotel import search_hotels, get_hotel_tools

def get_all_tools():
    """获取所有外部工具"""
    return get_weather_tools() + get_map_tools() + get_hotel_tools() + get_time_tools()

def get_safe_tools():
    """获取无需敏感审批的只读工具。"""
    return get_weather_tools() + get_map_tools() + get_hotel_tools() + get_time_tools()

__all__ = ["get_all_tools", "get_safe_tools", "get_weather", "get_route", "search_hotels", "get_current_time"]
