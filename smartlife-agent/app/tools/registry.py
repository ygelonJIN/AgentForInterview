"""当前主链路使用的统一、Actor 绑定工具注册表。"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

from langchain_core.tools import BaseTool


class ToolRegistry:
    """去重并统一暴露 canonical 工具与领域 Tool Provider。"""

    def __init__(self, tools: Optional[Sequence[BaseTool]] = None):
        self._tools: dict[str, BaseTool] = {}
        for item in tools or []:
            self.register(item)

    def register(self, tool: BaseTool, *, replace: bool = False) -> None:
        name = getattr(tool, "name", "")
        if not name:
            raise ValueError("工具必须有 name")
        if name in self._tools and not replace:
            raise ValueError(f"工具重复注册: {name}")
        self._tools[name] = tool

    def get_tool_by_name(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def tools(self, names: Optional[Iterable[str]] = None) -> List[BaseTool]:
        if names is None:
            return list(self._tools.values())
        requested = list(names)
        missing = [name for name in requested if name not in self._tools]
        if missing:
            raise KeyError(f"未注册工具: {missing}")
        return [self._tools[name] for name in requested]

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "args_schema": getattr(tool, "args_schema", None).__name__
                if getattr(tool, "args_schema", None) is not None
                else None,
            }
            for tool in self._tools.values()
        ]


def build_tool_registry(
    *,
    actor_id: Optional[str] = None,
    include_write_tools: bool = False,
) -> ToolRegistry:
    """构建注册表；用户数据工具只对已绑定 Actor 暴露。"""
    from app.tools.map import get_route
    from app.tools.time import get_current_time
    from app.tools.weather import get_weather
    from app.tools.memory_tools import MemoryToolProvider
    from app.tools.shopping_tools import ShoppingToolProvider
    from app.tools.travel_tools import TravelToolProvider

    actor = str(actor_id or "").strip()
    registry = ToolRegistry()
    for item in (get_weather, get_route, get_current_time):
        registry.register(item)

    shopping_tools = ShoppingToolProvider(actor_id=actor or None).get_tools()
    for item in shopping_tools:
        if item.name != "get_order_status" or actor:
            registry.register(item)

    for item in TravelToolProvider().get_tools():
        if registry.get_tool_by_name(item.name) is None:
            registry.register(item)

    if actor:
        safe_memory_names = {"get_user_profile", "get_cross_scene_memories", "get_user_context"}
        for item in MemoryToolProvider(actor_id=actor).get_tools():
            if include_write_tools or item.name in safe_memory_names:
                if registry.get_tool_by_name(item.name) is None:
                    registry.register(item)
    return registry


def get_registry(
    *,
    actor_id: Optional[str] = None,
    include_write_tools: bool = False,
) -> ToolRegistry:
    return build_tool_registry(
        actor_id=actor_id,
        include_write_tools=include_write_tools,
    )
