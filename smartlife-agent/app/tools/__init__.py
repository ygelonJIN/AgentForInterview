from .weather import get_weather, get_weather_tools
from .map import get_route, get_map_tools
from .payment import get_payment_tools
from .time import get_current_time, get_time_tools


def get_all_tools(actor_id: str | None = None, agent_id: str | None = None):
    """返回工具注册表中的全部非支付工具，可按 Agent 能力面隔离。"""
    from .registry import get_registry
    return get_registry(
        actor_id=actor_id,
        include_write_tools=True,
        agent_id=agent_id,
    ).tools()


def get_safe_tools(actor_id: str | None = None, agent_id: str | None = None):
    """返回当前 Agent 可用的只读工具；指定 Agent 时执行严格隔离。"""
    from .registry import get_registry
    return get_registry(
        actor_id=actor_id,
        include_write_tools=False,
        agent_id=agent_id,
    ).tools()


__all__ = [
    "get_all_tools",
    "get_safe_tools",
    "get_weather",
    "get_route",
    "get_current_time",
]
