"""Agent 清单一致性：清单、能力面、路由、MCP 四处必须对得上。"""

import pytest

from app.agents.specs import AGENT_SPECS, agent_ids, get_spec, iter_specs
from app.graphs.nodes import ROUTE_KEYS, route
from app.graphs.orchestrator import ROUTE_TARGETS
from app.tools.agent_capabilities import AGENT_TOOL_POLICIES


def test_specs_and_policies_cover_the_same_agents():
    assert set(AGENT_SPECS) == set(AGENT_TOOL_POLICIES)
    assert set(agent_ids()) == ROUTE_KEYS
    # 顶层图的路由表是字面量（给静态文档解析用），必须与清单一致
    assert set(ROUTE_TARGETS) == set(agent_ids())


def test_spec_names_are_aligned_three_ways():
    for spec in iter_specs():
        policy = AGENT_TOOL_POLICIES[spec.agent_id]
        assert spec.builder.startswith("app.graphs.subgraphs.")
        assert spec.builder.endswith(":build_" + spec.agent_id + "_graph")
        assert spec.build() is not None
        assert policy.agent_id == spec.agent_id
        assert policy.mcp_server_name.endswith("agent")


def test_route_honours_explicit_agent():
    for agent in ROUTE_KEYS:
        assert route({"forced_agent": agent}) == agent


def test_unknown_agent_is_rejected():
    with pytest.raises(KeyError):
        get_spec("weather_bot")


def test_every_spec_builds_its_graph():
    from app.graphs.orchestrator import build_turn_graph

    nodes = set(build_turn_graph().get_graph().nodes)
    assert nodes == ROUTE_KEYS | {"__start__", "__end__", "prepare", "classify", "finalize"}
