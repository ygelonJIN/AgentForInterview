"""当前主链路使用的统一、Actor 绑定、Agent 隔离工具注册表。"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from langchain_core.tools import BaseTool

from app.tools.agent_capabilities import (
    AgentToolPolicy,
    get_agent_tool_policy,
    validate_agent_tools,
)


class ToolRegistry:
    """去重并统一暴露 canonical 工具与领域 Tool Provider。

    指定 ``agent_id`` 后，注册表成为该 Agent 的独立能力面：任何不属于它的
    工具都会在注册阶段被拒绝，而不只是从模型提示词里隐藏。
    """

    def __init__(
        self,
        tools: Optional[Sequence[BaseTool]] = None,
        *,
        agent_id: Optional[str] = None,
    ):
        self.agent_policy: Optional[AgentToolPolicy] = (
            get_agent_tool_policy(agent_id) if agent_id else None
        )
        self._tools: dict[str, BaseTool] = {}
        for item in tools or []:
            self.register(item)

    @property
    def agent_id(self) -> Optional[str]:
        return self.agent_policy.agent_id if self.agent_policy else None

    def register(self, tool: BaseTool, *, replace: bool = False) -> None:
        name = getattr(tool, "name", "")
        if not name:
            raise ValueError("工具必须有 name")
        if self.agent_policy is not None:
            self.agent_policy.require(name)
        if name in self._tools and not replace:
            raise ValueError(f"工具重复注册: {name}")
        self._tools[name] = tool

    def get_tool_by_name(self, name: str) -> Optional[BaseTool]:
        if self.agent_policy is not None and not self.agent_policy.allows(name):
            return None
        return self._tools.get(name)

    def tools(self, names: Optional[Iterable[str]] = None) -> List[BaseTool]:
        if names is None:
            return list(self._tools.values())
        requested = list(names)
        if self.agent_policy is not None:
            for name in requested:
                self.agent_policy.require(name)
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
    agent_id: Optional[str] = None,
) -> ToolRegistry:
    """构建工具注册表；用户数据工具只对已绑定 Actor 暴露。

    ``agent_id`` 是即插即用能力面参数：

    - ``shopping``：只注册电商工具；
    - ``travel``：只注册旅游、天气、路线和时间工具；
    - ``general``：只注册通用天气、路线和时间工具；
    - 省略：保留旧的统一 MCP Registry 行为。
    """
    from app.tools.map import get_route
    from app.tools.time import get_current_time
    from app.tools.weather import get_weather
    from app.tools.memory_tools import MemoryToolProvider
    from app.tools.shopping_tools import ShoppingToolProvider
    from app.tools.travel_tools import TravelToolProvider

    actor = str(actor_id or "").strip()
    policy = get_agent_tool_policy(agent_id) if agent_id else None
    registry = ToolRegistry(agent_id=agent_id)

    def add(item: BaseTool) -> None:
        if policy is not None and not policy.allows(item.name):
            return
        if registry.get_tool_by_name(item.name) is not None:
            return
        registry.register(item)

    for item in (get_weather, get_route, get_current_time):
        add(item)

    shopping_tools = ShoppingToolProvider(actor_id=actor or None).get_tools()
    for item in shopping_tools:
        if item.name != "get_order_status" or actor:
            add(item)

    for item in TravelToolProvider().get_tools():
        add(item)

    if actor and (policy is None or policy.agent_id != "travel"):
        safe_memory_names = {"get_user_profile", "get_cross_scene_memories", "get_user_context"}
        for item in MemoryToolProvider(actor_id=actor).get_tools():
            if include_write_tools or item.name in safe_memory_names:
                add(item)

    if policy is not None:
        validate_agent_tools(policy.agent_id, registry.tools())
    return registry


def get_registry(
    *,
    actor_id: Optional[str] = None,
    include_write_tools: bool = False,
    agent_id: Optional[str] = None,
) -> ToolRegistry:
    return build_tool_registry(
        actor_id=actor_id,
        include_write_tools=include_write_tools,
        agent_id=agent_id,
    )
