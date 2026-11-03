"""Agent 即插即用能力面、MCP 发现和运行时工具隔离测试。"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.graphs.context import TurnContext
from app.graphs.subgraphs.react import build_react_graph, initial_react_state
from app.mcp_servers.protocol_server import build_mcp_server
from app.tools.agent_capabilities import (
    AGENT_TOOL_POLICIES,
    ToolAuthorizationError,
    get_agent_tool_policy,
    validate_agent_tools,
)
from app.tools.registry import build_tool_registry
from app.tools.shopping_tools import ShoppingToolProvider
from app.tools.travel_tools import TravelToolProvider


def test_shopping_and_travel_tool_policies_are_disjoint_and_complete():
    shopping = ShoppingToolProvider().get_tools()
    travel = TravelToolProvider().get_tools()

    assert {tool.name for tool in shopping} == get_agent_tool_policy("shopping").tool_names
    assert {tool.name for tool in travel} <= get_agent_tool_policy("travel").tool_names
    assert get_agent_tool_policy("shopping").tool_names.isdisjoint(
        get_agent_tool_policy("travel").tool_names
    )
    assert set(AGENT_TOOL_POLICIES) == {"shopping", "travel", "general"}


def test_agent_registry_plug_in_exposes_only_its_tools():
    shopping = build_tool_registry(agent_id="shopping", actor_id="user_001")
    travel = build_tool_registry(agent_id="travel", actor_id="user_001")

    shopping_names = {tool.name for tool in shopping.tools()}
    travel_names = {tool.name for tool in travel.tools()}

    assert shopping_names == {"search_products", "get_product_reviews", "get_order_status"}
    assert travel_names == {
        "search_destinations",
        "get_local_activities",
        "plan_itinerary",
        "get_weather",
        "get_route",
        "get_current_time",
    }
    assert shopping_names.isdisjoint(travel_names)


def test_agent_registry_rejects_foreign_tools_at_registration():
    shopping_tool = ShoppingToolProvider().get_tool_by_name("search_products")
    travel_tool = TravelToolProvider().get_tool_by_name("search_destinations")

    validate_agent_tools("shopping", [shopping_tool])
    try:
        validate_agent_tools("shopping", [shopping_tool, travel_tool])
    except ToolAuthorizationError as exc:
        assert "search_destinations" in str(exc)
    else:
        raise AssertionError("跨 Agent 工具没有被拒绝")


def test_mcp_servers_are_independent_capability_surfaces():
    shopping = build_mcp_server(agent_id="shopping", actor_id="user_001")
    travel = build_mcp_server(agent_id="travel", actor_id="user_001")

    shopping_names = asyncio.run(_tool_names(shopping))
    travel_names = asyncio.run(_tool_names(travel))

    assert shopping.name == "smartlife-shopping-agent"
    assert travel.name == "smartlife-travel-agent"
    assert "search_products" in shopping_names
    assert "get_route" not in shopping_names
    assert "get_route" in travel_names
    assert "search_products" not in travel_names


def test_model_tool_call_cannot_cross_agent_boundary():
    class ShoppingTool:
        name = "search_products"
        calls = []

        async def ainvoke(self, args):
            self.calls.append(args)
            return {"ok": True}

    class InjectedModel:
        def bind_tools(self, tools):
            self.bound_tool_names = {tool.name for tool in tools}
            return self

        async def ainvoke(self, _messages):
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "get_route",
                    "args": {"origin": "上海", "destination": "杭州"},
                    "id": "injected-call",
                }],
            )

    tool = ShoppingTool()
    model = InjectedModel()

    class _Services:
        llm = model

        def tools_for(self, actor_id, agent_id="general"):
            return [tool]

    ctx = TurnContext(services=_Services(), trace_id="isolation").with_extra(
        agent_id="shopping", actor_id="user_001"
    )
    result = asyncio.run(build_react_graph().ainvoke(
        initial_react_state([HumanMessage(content="越权调用")], max_steps=3, tool_timeout=1.0),
        context=ctx,
    ))

    assert model.bound_tool_names == {"search_products"}
    assert tool.calls == []
    assert result["tool_results"][0]["ok"] is False
    assert "工具越权" in result["tool_results"][0]["error"]
    assert "get_route" in result["tool_results"][0]["error"]


def test_mcp_call_rejects_tool_from_other_agent():
    from mcp.server.mcpserver.exceptions import ToolError

    shopping = build_mcp_server(agent_id="shopping", actor_id="user_001")
    with pytest.raises(ToolError, match="Unknown tool: get_route"):
        asyncio.run(shopping.call_tool("get_route", {"origin": "上海", "destination": "杭州"}))


async def _tool_names(server):
    return {tool.name for tool in await server.list_tools()}
