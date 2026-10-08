from .weather import get_weather, get_weather_tools
from .map import get_route, get_map_tools
from .payment import get_payment_tools
from .time import get_current_time, get_time_tools


def get_all_tools():
    """返回统一注册表中的全部非支付工具。"""
    from .registry import get_registry
    return get_registry(include_write_tools=True).tools()


def get_safe_tools():
    """返回当前 ReAct 主链路可用的只读工具。"""
    from .registry import get_registry
    return get_registry(include_write_tools=False).tools()


__all__ = [
    "get_all_tools",
    "get_safe_tools",
    "get_weather",
    "get_route",
    "get_current_time",
]
