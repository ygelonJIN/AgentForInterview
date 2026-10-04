from .weather import get_weather, get_weather_tools
from .map import get_route, get_map_tools
from .payment import create_payment, get_payment_status, get_payment_tools
from .time import get_current_time, get_time_tools

def get_all_tools():
    """获取所有外部工具"""
    return get_weather_tools() + get_map_tools() + get_payment_tools() + get_time_tools()

__all__ = ["get_all_tools", "get_weather", "get_route", "create_payment", "get_payment_status", "get_current_time"]
