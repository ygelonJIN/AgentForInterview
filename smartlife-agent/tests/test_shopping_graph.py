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

    async def generate_shopping_response(
        self,
        message,
        user_id,
        thread_id,
        classification,
        retrieval,
        queue,
        validation_feedback="",
    ):
        self.calls.append((
            "generate",
            message,
            user_id,
            thread_id,
            retrieval,
            validation_feedback,
        ))
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


def test_validation_budget_covers_one_full_regeneration():
    graph = ShoppingGraph(_FakeLegacy())

    assert graph.node_policies["generate"].timeout_seconds == 120
    assert graph.node_policies["validate"].timeout_seconds == 120


def test_shopping_graph_state_has_no_runtime_queue():
    graph = ShoppingGraph(_FakeLegacy())
    state_schema = graph.graph.builder.schemas.get("state", {})
    assert "queue" not in state_schema


def test_shopping_response_must_reference_retrieved_product_name():
    retrieval = {
        "needs": {"needs_product": True, "needs_review": False},
        "product_context": "真实跑鞋 | ¥399",
        "review_context": "",
        "products": [{"name": "真实跑鞋", "price": 399}],
        "diagnostics": [],
    }

    OrchestratorV2.validate_shopping_response("推荐真实跑鞋，价格 ¥399", retrieval)
    with pytest.raises(ValueError, match="真实商品名称"):
        OrchestratorV2.validate_shopping_response("推荐一款虚构跑鞋", retrieval)


def test_missing_product_evidence_requires_explicit_not_found_response():
    retrieval = {
        "needs": {"needs_product": True, "needs_review": False},
        "product_context": "",
        "products": [],
        "diagnostics": ["NL2SQL 未返回商品结果"],
    }

    OrchestratorV2.validate_shopping_response(
        "暂未找到符合条件的商品，请放宽条件",
        retrieval,
    )
    with pytest.raises(ValueError, match="暂未找到"):
        OrchestratorV2.validate_shopping_response("推荐一款跑鞋", retrieval)


def test_failed_review_retrieval_must_be_disclosed():
    retrieval = {
        "needs": {"needs_product": False, "needs_review": True},
        "product_context": "",
        "review_context": "",
        "products": [],
        "diagnostics": ["RAG 检索失败: timeout"],
    }

    with pytest.raises(ValueError, match="评价信息不完整"):
        OrchestratorV2.validate_shopping_response("这双鞋口碑很好", retrieval)
    OrchestratorV2.validate_shopping_response(
        "当前评价检索失败，评价信息不完整，无法确认口碑。",
        retrieval,
    )


def test_shopping_graph_regenerates_once_after_validation_failure():
    class _RetryLegacy(_FakeLegacy):
        async def retrieve_shopping_context(self, message, strategy, needs, queue):
            self.calls.append(("retrieve", message, strategy, needs))
            return {
                "product_context": "真实跑鞋 | ¥399",
                "review_context": "",
                "products": [{"name": "真实跑鞋", "price": 399}],
                "diagnostics": [],
                "needs": {"needs_product": True, "needs_review": False},
            }

        async def generate_shopping_response(
            self,
            message,
            user_id,
            thread_id,
            classification,
            retrieval,
            queue,
            validation_feedback="",
        ):
            self.calls.append(("generate", validation_feedback))
            return "推荐虚构跑鞋" if not validation_feedback else "推荐真实跑鞋"

    legacy = _RetryLegacy()
    response = _run(ShoppingGraph(legacy), legacy)

    assert response == "推荐真实跑鞋"
    generate_calls = [call for call in legacy.calls if call[0] == "generate"]
    assert [call[1] for call in generate_calls] == ["", "回答必须至少引用一个检索到的真实商品名称"]
