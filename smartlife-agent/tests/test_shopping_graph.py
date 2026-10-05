"""ShoppingGraph 状态、节点顺序和输出契约测试。"""

import asyncio

import pytest

from app.agents.orchestrator_v2 import OrchestratorV2
from app.agents.shopping_graph import ShoppingGraph


class _FakeLegacy:
    def __init__(self):
        self.calls = []

    async def plan_shopping_retrieval(self, message, classification, queue):
        self.calls.append(("plan", message, classification.intent))
        return {"needs_product": True, "needs_review": False, "strategy": "sql_only"}, "sql_only"

    async def retrieve_shopping_context(self, message, strategy, needs, queue):
        self.calls.append(("retrieve", message, strategy, needs))
        return {"product_context": "真实商品", "review_context": "", "diagnostics": []}

    async def generate_shopping_response(self, message, user_id, thread_id, classification, retrieval, queue):
        self.calls.append(("generate", message, user_id, thread_id, retrieval))
        return "推荐真实商品"

    def validate_shopping_response(self, response, retrieval):
        self.calls.append(("validate", response, retrieval))
        OrchestratorV2.validate_shopping_response(response, retrieval)


def _run(graph, legacy):
    async def run():
        return await graph.run_streaming(
            "推荐跑鞋",
            "user-1",
            "user-1:shopping:browser-1",
            {"intent": "shopping", "route": "react"},
            object(),
        )
    return asyncio.run(run())


def test_shopping_graph_runs_all_nodes_and_returns_response():
    legacy = _FakeLegacy()
    graph = ShoppingGraph(legacy)

    response = _run(graph, legacy)

    assert response == "推荐真实商品"
    assert [call[0] for call in legacy.calls] == ["plan", "retrieve", "generate", "validate"]
    assert legacy.calls[1][2] == "sql_only"
    assert legacy.calls[2][3] == "user-1:shopping:browser-1"


def test_shopping_graph_rejects_empty_response():
    class _EmptyLegacy(_FakeLegacy):
        async def generate_shopping_response(self, *args, **kwargs):
            return "   "

    legacy = _EmptyLegacy()
    graph = ShoppingGraph(legacy)

    with pytest.raises(ValueError, match="购物回答为空"):
        _run(graph, legacy)


def test_shopping_graph_state_has_no_runtime_queue():
    graph = ShoppingGraph(_FakeLegacy())
    state_schema = graph.graph.builder.schemas.get("state", {})
    assert "queue" not in state_schema
