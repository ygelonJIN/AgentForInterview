"""LangSmith Studio / Agent Server 入口。

`build_turn_graph(checkpointer=None)` 是项目内部用的工厂，带 checkpointer 形参。
langgraph-api 会按签名往这个形参里塞平台自己的配置对象（dict），
再传给 `graph.compile(checkpointer=...)` 就会报 Invalid checkpointer。

所以这里用零参包装：不暴露任何形参，持久化完全交给平台层。
"""
from __future__ import annotations

from app.graphs.orchestrator import build_turn_graph


def turn_graph():
    """返回编译好的顶层图，不带 checkpointer。"""
    return build_turn_graph(None)
