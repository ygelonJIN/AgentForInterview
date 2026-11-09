"""工具包的公开出口。

只 re-export 规范工具对象，不再提供按域拼列表的 ``get_*_tools`` shim
（那些 shim 无人调用，注册统一走 ``app.tools.registry``）。
"""

from .weather import get_weather, get_weather_forecast
from .map import get_route
from .time import get_current_time


def get_all_tools(actor_id: str | None = None, agent_id: str | None = None):
    """返回工具注册表中的全部工具（含记忆写入），可按 Agent 能力面隔离。"""
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
    "get_weather_forecast",
    "get_route",
    "get_current_time",
]
