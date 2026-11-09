"""Agent 专属工具能力面与严格隔离策略。

这里的策略同时约束三层：

1. MCP ``tools/list`` 只能看到当前 Agent 的工具；
2. 模型 Tool Calling 只会 bind 当前 Agent 的工具；
3. 即使模型被注入、越狱或返回伪造 tool_call，执行层也会拒绝跨 Agent 工具。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


class ToolAuthorizationError(PermissionError):
    """模型或调用方尝试使用不属于当前 Agent 的工具。"""


@dataclass(frozen=True)
class AgentToolPolicy:
    agent_id: str
    display_name: str
    description: str
    tool_names: frozenset[str]
    mcp_server_name: str

    def allows(self, tool_name: str) -> bool:
        return str(tool_name or "") in self.tool_names

    def require(self, tool_name: str) -> None:
        name = str(tool_name or "")
        if not self.allows(name):
            raise ToolAuthorizationError(
                f"工具越权: {name or '<empty>'} 不属于 Agent {self.agent_id}"
            )


# 电商和旅游工具集合严格互斥，避免模型跨域乱调工具。
AGENT_TOOL_POLICIES: dict[str, AgentToolPolicy] = {
    "shopping": AgentToolPolicy(
        agent_id="shopping",
        display_name="电商购物 Agent",
        description="商品、评价和当前用户订单工具",
        tool_names=frozenset({
            "search_products",
            "get_product_reviews",
            "get_order_status",
        }),
        mcp_server_name="smartlife-shopping-agent",
    ),
    "travel": AgentToolPolicy(
        agent_id="travel",
        display_name="旅游出行 Agent",
        description="目的地、活动、行程、天气和路线工具",
        tool_names=frozenset({
            "search_destinations",
            "get_local_activities",
            "plan_itinerary",
            "get_weather",
            "get_weather_forecast",
            "get_air_quality",
            "get_exchange_rate",
            "get_public_holidays",
            "get_route",
            "get_current_time",
        }),
        mcp_server_name="smartlife-travel-agent",
    ),
    "general": AgentToolPolicy(
        agent_id="general",
        display_name="通用助手 Agent",
        description="仅使用通用天气、路线和时间工具",
        tool_names=frozenset({
            "get_weather",
            "get_route",
            "get_current_time",
        }),
        mcp_server_name="smartlife-general-agent",
    ),
}

_AGENT_ALIASES = {
    "shopping-agent": "shopping",
    "shopping_agent": "shopping",
    "travel-agent": "travel",
    "travel_agent": "travel",
    "general-agent": "general",
    "general_agent": "general",
}


def normalize_agent_id(agent_id: str) -> str:
    value = str(agent_id or "").strip().lower()
    value = _AGENT_ALIASES.get(value, value)
    if value not in AGENT_TOOL_POLICIES:
        supported = "、".join(sorted(AGENT_TOOL_POLICIES))
        raise ValueError(f"未知 Agent 能力面: {agent_id!r}，仅支持 {supported}")
    return value


def get_agent_tool_policy(agent_id: str) -> AgentToolPolicy:
    return AGENT_TOOL_POLICIES[normalize_agent_id(agent_id)]


def validate_agent_tools(
    agent_id: str,
    tools: Sequence[Any],
    *,
    require_all: bool = False,
) -> AgentToolPolicy:
    """验证工具集合完全属于当前 Agent，可选要求完整能力清单。"""
    policy = get_agent_tool_policy(agent_id)
    names = [str(getattr(tool, "name", "") or "") for tool in tools]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Agent {policy.agent_id} 工具重复注册: {duplicates}")

    foreign = sorted({name for name in names if name and not policy.allows(name)})
    if foreign:
        raise ToolAuthorizationError(
            f"Agent {policy.agent_id} 混入越权工具: {foreign}"
        )

    if require_all:
        missing = sorted(policy.tool_names - set(names))
        if missing:
            raise ValueError(f"Agent {policy.agent_id} 缺少能力工具: {missing}")
    return policy
