"""Agent 清单：一个 Agent = 一个图节点 = 一条工具白名单 = 一个 MCP Server。

加一个 Agent 只需要改两处：本文件加一条 ``AgentSpec``，
``agent_capabilities.AGENT_TOOL_POLICIES`` 加一条白名单。
顶层图、路由表、MCP 的 ``--agent`` 取值都从这里派生。

构图函数用 ``"模块:函数"`` 字符串声明而不是直接引用：这样
``scripts/build_pipeline_spec.py`` 的静态解析（只读 AST，不 import 业务模块）
也能读出 Agent 与子图的对应关系。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator


BASE_INTENTS = ("shopping", "travel", "customer_service", "general")


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    display_name: str
    description: str
    builder: str
    # 自然语言入口：复用四个基础意图之一，或一个新的意图名，或 forced_only（只能点名）。
    intent: str = "general"
    output: str = "text"
    enabled: bool = True
    _resolved: Any = field(default=None, repr=False, compare=False)

    @property
    def builder_func(self) -> Callable[[], Any]:
        """懒加载构图函数：``"app.graphs.subgraphs.shopping:build_shopping_graph"``。"""
        if self._resolved is not None:
            return self._resolved
        module_name, _, func_name = self.builder.partition(":")
        if not module_name or not func_name:
            raise ValueError(f"AgentSpec.builder 必须写成 '模块:函数'：{self.builder!r}")
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
        object.__setattr__(self, "_resolved", func)
        return func

    def build(self) -> Any:
        return self.builder_func()


BUILTIN_SPECS: Dict[str, AgentSpec] = {
    spec.agent_id: spec
    for spec in (
        AgentSpec(
            agent_id="shopping",
            display_name="电商购物 Agent",
            description="商品检索、评价与订单查询",
            builder="app.graphs.subgraphs.shopping:build_shopping_graph",
        ),
        AgentSpec(
            agent_id="travel",
            display_name="旅行 Agent",
            description="旅行问答；多天行程走无装备版行程图",
            builder="app.graphs.subgraphs.travel:build_travel_graph",
        ),
        AgentSpec(
            agent_id="trip_gear",
            display_name="行程装备协同 Agent",
            description="行程 + 装备 + 预算 + 校验",
            builder="app.graphs.subgraphs.trip_gear:build_trip_gear_graph",
        ),
        AgentSpec(
            agent_id="general",
            display_name="通用助手 Agent",
            description="通用问答与实时事实查询",
            builder="app.graphs.subgraphs.general:build_general_graph",
        ),
        AgentSpec(
            agent_id="customer_service",
            display_name="订单客服 Agent",
            description="只读订单状态",
            builder="app.graphs.subgraphs.customer_service:build_customer_service_graph",
        ),
    )
}


AGENT_SPECS: Dict[str, AgentSpec] = {**BUILTIN_SPECS}


def allowed_intents() -> list[str]:
    values = set(BASE_INTENTS)
    for spec in AGENT_SPECS.values():
        intent = str(spec.intent or "")
        if intent and intent != "forced_only":
            values.add(intent)
    return sorted(values)


def agent_for_intent(intent: str) -> str:
    """某个新意图挂着的 Agent；四个基础意图返回空（由 route 的既有分支处理）。"""
    key = str(intent or "").strip()
    if not key or key in BASE_INTENTS:
        return ""
    from app.agents.config import is_enabled

    for spec in AGENT_SPECS.values():
        if spec.intent == key and is_enabled(spec.agent_id):
            return spec.agent_id
    return ""


def agent_ids(*, enabled_only: bool = False) -> list[str]:
    if not enabled_only:
        return [spec.agent_id for spec in AGENT_SPECS.values()]
    from app.agents.config import is_enabled

    return [spec.agent_id for spec in AGENT_SPECS.values() if is_enabled(spec.agent_id)]


def iter_specs(*, enabled_only: bool = False) -> Iterator[AgentSpec]:
    if not enabled_only:
        return iter(AGENT_SPECS.values())
    from app.agents.config import is_enabled

    return iter(spec for spec in AGENT_SPECS.values() if is_enabled(spec.agent_id))


def export_spec(agent_id: str) -> AgentSpec:
    return get_spec(agent_id)


def get_spec(agent_id: str) -> AgentSpec:
    key = str(agent_id or "").strip().lower()
    if key not in AGENT_SPECS:
        raise KeyError(f"未知 Agent: {agent_id!r}")
    return AGENT_SPECS[key]
