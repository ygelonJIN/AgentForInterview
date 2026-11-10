"""工具可选择：配置覆盖白名单、自定义工具进全局池、隔离仍然生效。"""

import pytest

from app.agents import config as agent_config
from app.tools import custom as custom_module
from app.tools.agent_capabilities import get_agent_tool_policy
from app.tools.registry import build_tool_registry


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_config, "CONFIG_PATH", tmp_path / "agent_config.json")
    monkeypatch.setattr(custom_module, "CUSTOM_DIR", tmp_path / "tools")
    custom_module._reset()
    yield tmp_path
    custom_module._reset()


def test_default_policy_is_used_without_config(isolated):
    assert get_agent_tool_policy("shopping").tool_names == frozenset(
        {"search_products", "get_product_reviews", "get_order_status"}
    )


def test_selected_tools_replace_default_whitelist(isolated):
    agent_config.set_tools("shopping", ["get_product_reviews"])

    policy = get_agent_tool_policy("shopping")
    assert policy.tool_names == frozenset({"get_product_reviews"})
    registry = build_tool_registry(agent_id="shopping", actor_id="u1")
    assert {tool.name for tool in registry.tools()} == {"get_product_reviews"}


def test_selection_can_add_a_global_tool(isolated):
    agent_config.set_tools("general", ["get_weather", "get_time_offset"])

    registry = build_tool_registry(agent_id="general", actor_id="u1")

    assert {"get_weather", "get_time_offset"} == {tool.name for tool in registry.tools()}


def test_custom_tool_enters_pool_only_when_selected(isolated):
    tools_dir = isolated / "tools"
    tools_dir.mkdir()
    (tools_dir / "hello.py").write_text(
        "from langchain_core.tools import tool\n\n"
        "@tool('hello_tool')\n"
        "def hello_tool(name: str) -> str:\n"
        "    '打个招呼'\n"
        "    return f'hi {name}'\n\n"
        "TOOLS = [hello_tool]\n",
        encoding="utf-8",
    )
    custom_module._reset()

    assert custom_module.custom_tool_names() == ["hello_tool"]

    # 没选它：不注册
    assert "hello_tool" not in {
        tool.name for tool in build_tool_registry(agent_id="general", actor_id="u1").tools()
    }
    # 选它：注册且只对 general 可用
    agent_config.set_tools("general", ["get_weather", "hello_tool"])
    general = build_tool_registry(agent_id="general", actor_id="u1")
    shopping = build_tool_registry(agent_id="shopping", actor_id="u1")
    assert "hello_tool" in {tool.name for tool in general.tools()}
    assert "hello_tool" not in {tool.name for tool in shopping.tools()}


def test_broken_custom_tool_file_does_not_break_pool(isolated):
    tools_dir = isolated / "tools"
    tools_dir.mkdir()
    (tools_dir / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    custom_module._reset()

    assert custom_module.load_custom_tools() == {}
    assert (tools_dir / "broken.broken").exists()


def test_reset_restores_defaults(isolated):
    agent_config.set_tools("shopping", ["search_products"])
    agent_config.reset()

    assert get_agent_tool_policy("shopping").tool_names == frozenset(
        {"search_products", "get_product_reviews", "get_order_status"}
    )
