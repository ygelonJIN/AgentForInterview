"""Agent 专属工具能力面与严格隔离策略。

这里的策略同时约束两层：

1. 模型 Tool Calling 只会 bind 当前 Agent 的工具；
2. 即使模型被注入、越狱或返回伪造 tool_call，执行层也会拒绝跨 Agent 工具。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Sequence


class ToolAuthorizationError(PermissionError):
    """模型或调用方尝试使用不属于当前 Agent 的工具。"""


@dataclass(frozen=True)
class AgentToolPolicy:
    agent_id: str
    display_name: str
    description: str
    tool_names: frozenset[str]

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
    ),
    "travel": AgentToolPolicy(
        agent_id="travel",
        display_name="旅游出行 Agent",
        description="目的地、活动、行程、天气和路线工具",
        tool_names=frozenset({
            "search_destinations",
            "get_local_activities",
            "get_weather",
            "get_weather_forecast",
            "get_air_quality",
            "get_exchange_rate",
            "get_public_holidays",
            "get_route",
            "get_current_time",
        }),
    ),
    "trip_gear": AgentToolPolicy(
        agent_id="trip_gear",
        display_name="行程装备协同 Agent",
        description="行程事实（天气/空气/汇率/假日/路线/时差）与装备协同",
        tool_names=frozenset({
            "get_weather_forecast",
            "get_air_quality",
            "get_exchange_rate",
            "get_public_holidays",
            "get_route",
            "get_time_offset",
            "search_products",
            "get_product_reviews",
        }),
    ),
    "customer_service": AgentToolPolicy(
        agent_id="customer_service",
        display_name="订单客服 Agent",
        description="只读当前用户的订单状态",
        tool_names=frozenset({"get_order_status"}),
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
    ),
}

_AGENT_ALIASES = {
    "shopping-agent": "shopping",
    "shopping_agent": "shopping",
    "travel-agent": "travel",
    "travel_agent": "travel",
    "general-agent": "general",
    "general_agent": "general",
    "trip-gear": "trip_gear",
    "trip_gear_agent": "trip_gear",
    "tripgear": "trip_gear",
    "customer-service": "customer_service",
    "customer_service_agent": "customer_service",
    "service": "customer_service",
}


def normalize_agent_id(agent_id: str) -> str:
    value = str(agent_id or "").strip().lower()
    value = _AGENT_ALIASES.get(value, value)
    if value not in AGENT_TOOL_POLICIES:
        supported = "、".join(sorted(AGENT_TOOL_POLICIES))
        raise ValueError(f"未知 Agent 能力面: {agent_id!r}，仅支持 {supported}")
    return value


def default_tool_names(agent_id: str) -> frozenset[str]:
    return AGENT_TOOL_POLICIES[normalize_agent_id(agent_id)].tool_names


def get_agent_tool_policy(agent_id: str) -> AgentToolPolicy:
    """默认白名单 + 用户在配置页选过的工具。

    没配过就用默认；配过就完全以配置为准（可加也可减），
    这样"每个 Agent 用哪些工具"是数据，不是代码。
    """
    policy = AGENT_TOOL_POLICIES[normalize_agent_id(agent_id)]
    try:
        from app.agents.config import tools_for

        chosen = tools_for(policy.agent_id)
    except Exception:
        chosen = None
    if chosen is None:
        return policy
    return replace(policy, tool_names=frozenset(chosen))


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
