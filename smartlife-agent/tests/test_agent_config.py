"""Agent 启停配置：落盘、读回、路由生效、恢复默认。"""

from pathlib import Path

import pytest

from app.agents import config as agent_config
from app.graphs.nodes import route


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "agent_config.json"
    monkeypatch.setattr(agent_config, "CONFIG_PATH", path)
    return path


def test_default_is_enabled(isolated_config):
    assert agent_config.is_enabled("shopping") is True
    assert not isolated_config.exists()


def test_disable_persists_and_gates_routing(isolated_config):
    agent_config.set_enabled("shopping", False)

    assert isolated_config.exists()
    assert agent_config.is_enabled("shopping") is False
    # 点名 shopping 也会被兜回 general，而不是悄悄放行
    assert route({"forced_agent": "shopping", "user_message": "买帐篷"}) == "general"


def test_reset_restores_defaults(isolated_config):
    agent_config.set_enabled("travel", False)
    agent_config.reset()

    assert agent_config.is_enabled("travel") is True
    assert route({"forced_agent": "travel", "user_message": "杭州玩三天"}) == "travel"


def test_enabled_specs_follow_config(isolated_config):
    from app.agents.specs import agent_ids

    agent_config.set_enabled("customer_service", False)

    assert "customer_service" not in agent_ids(enabled_only=True)
    assert "customer_service" in agent_ids()
