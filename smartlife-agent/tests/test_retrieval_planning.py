"""证据需求规划测试。"""

from app.retrieval.planning import EvidenceNeed, EvidencePlanner


def test_planner_selects_only_requested_source():
    planner = EvidencePlanner()

    sql = planner.plan(strategy="sql_only", needs={"needs_product": True})
    rag = planner.plan(strategy="rag_only", needs={"needs_review": True})
    none = planner.plan(strategy="none")

    assert sql.selected_sources == ("sql",)
    assert rag.selected_sources == ("rag",)
    assert none.selected_sources == ()
    assert sql.derived_strategy == "sql_only"
    assert rag.derived_strategy == "rag_only"


def test_explicit_evidence_need_can_select_both_or_optional_sources():
    planner = EvidencePlanner()
    need = EvidenceNeed(
        facts=("product_price", "review_sentiment"),
        required_sources=("sql",),
        optional_sources=("rag",),
        allow_partial=True,
    )

    plan = planner.plan(evidence_need=need)

    assert plan.required_sources == ("sql",)
    assert plan.optional_sources == ("rag",)
    assert plan.selected_sources == ("sql", "rag")
    assert plan.derived_strategy == "mixed"


def test_planner_rejects_evidence_requirement_that_conflicts_with_compat_strategy():
    planner = EvidencePlanner()
    need = EvidenceNeed(facts=("review_sentiment",), required_sources=("rag",))

    try:
        planner.plan(strategy="sql_only", evidence_need=need)
    except ValueError as exc:
        assert "不一致" in str(exc)
    else:
        raise AssertionError("冲突计划必须被拒绝")


def test_retrieval_service_does_not_switch_to_an_unselected_source():
    from app.retrieval.service import RetrievalService

    class Sql:
        def __init__(self):
            self.calls = 0

        def query(self, _query):
            self.calls += 1
            raise RuntimeError("database unavailable")

    class Rag:
        def __init__(self):
            self.calls = 0

        def retrieve(self, _query, **_kwargs):
            self.calls += 1
            return {"documents": [], "ok": True, "error": None, "diagnostics": []}

    sql = Sql()
    rag = Rag()
    service = RetrievalService(sql, rag, reranker=None, cache_ttl_seconds=0)

    result = service.retrieve("500元以下跑步鞋", strategy="sql_only")

    assert sql.calls == 1
    assert rag.calls == 0
    assert result["effective_strategy"] == "sql_only"
    assert result["selected_sources"] == ["sql"]
    assert result["source_status"] == {
        "sql": "error",
        "rag": "not_selected",
        "tool": "not_selected",
        "memory": "not_selected",
    }
    assert result["missing_evidence"] == ["sql"]
    assert result["evidence_coverage"] == 0.0


def test_retrieval_service_supports_no_retrieval_plan():
    from app.retrieval.service import RetrievalService

    class Unexpected:
        def query(self, _query):
            raise AssertionError("SQL 不应被调用")

        def retrieve(self, _query, **_kwargs):
            raise AssertionError("RAG 不应被调用")

    service = RetrievalService(Unexpected(), Unexpected(), reranker=None)
    result = service.retrieve("你好", strategy="none")

    assert result["selected_sources"] == []
    assert result["evidence_coverage"] == 1.0
    assert result["sql_ok"] is None
    assert result["rag_ok"] is None


def test_shopping_needs_select_only_sql_or_only_rag_for_single_information_type():
    from app.agents.orchestrator_v2 import OrchestratorV2
    from app.classifier import ClassificationResult

    orchestrator = OrchestratorV2.__new__(OrchestratorV2)
    classification = ClassificationResult(
        intent="shopping",
        sub_intent="search",
        retrieval="mixed",
    )

    product_only = orchestrator._classify_data_needs("推荐一双 500 元以下的跑步鞋", classification)
    review_only = orchestrator._classify_data_needs("这双鞋的舒适度和耐用性怎么样", classification)
    combined = orchestrator._classify_data_needs("推荐一双 500 元以下且口碑好的跑步鞋", classification)

    assert product_only["strategy"] == "sql_only"
    assert product_only["required_sources"] == ["sql"]
    assert review_only["strategy"] == "rag_only"
    assert review_only["required_sources"] == ["rag"]
    assert combined["strategy"] == "mixed"
    assert set(combined["required_sources"]) == {"sql", "rag"}


def test_retrieval_service_supports_rerank_ab_control_and_treatment():
    from app.retrieval.service import RetrievalService

    class Reranker:
        def __init__(self):
            self.calls = 0

        def rerank(self, query, documents, top_n):
            self.calls += 1
            return documents[:top_n]

    class Sql:
        def query(self, _query):
            return {
                "results": [{"id": 1, "name": "跑步鞋", "description": "缓震"}],
                "count": 1,
            }

    class Rag:
        def retrieve(self, _query, **_kwargs):
            return {
                "documents": [{"content": "评价", "metadata": {}, "score": 0.1}],
                "ok": True,
                "error": None,
                "diagnostics": [],
            }

    reranker = Reranker()
    service = RetrievalService(Sql(), Rag(), reranker=reranker, cache_ttl_seconds=0)

    control = service.retrieve("跑步鞋评价", strategy="mixed", rerank_variant="control")
    treatment = service.retrieve("跑步鞋评价", strategy="mixed", rerank_variant="treatment")

    assert control["rerank"]["variant"] == "control"
    assert treatment["rerank"]["variant"] == "treatment"
    assert reranker.calls == 2


def test_orchestrator_logs_rerank_experiment_impression_when_enabled():
    import asyncio

    from app.agents.orchestrator_v2 import OrchestratorV2

    class Service:
        def retrieve(self, query, **kwargs):
            assert kwargs["rerank_variant"] in {"control", "treatment"}
            return {
                "products": [{"id": 9, "name": "跑步鞋"}],
                "documents": [{"content": "评价", "metadata": {"id": "doc-1"}}],
                "diagnostics": [],
                "selected_sources": ["sql", "rag"],
                "effective_strategy": "mixed",
                "strategy": "mixed",
                "needs": kwargs.get("needs") or {},
            }

    class Logger:
        def __init__(self):
            self.impressions = []

        def assign_variant(self, user_id, query):
            return "treatment"

        def log_impression(self, **kwargs):
            self.impressions.append(kwargs)

    class Queue:
        trace_id = "trace-1"

        async def emit_tool_call(self, *args, **kwargs):
            return None

    orchestrator = OrchestratorV2.__new__(OrchestratorV2)
    orchestrator._retrieval_service = Service()
    orchestrator._reranker = object()
    orchestrator._experiment_logger = Logger()
    result = asyncio.run(orchestrator._smart_retrieve(
        "跑步鞋评价",
        "mixed",
        Queue(),
        needs={"needs_product": True, "needs_review": True},
        owner="user-1",
    ))

    assert result["strategy"] == "mixed"
    assert len(orchestrator._experiment_logger.impressions) == 1
    assert orchestrator._experiment_logger.impressions[0]["variant"] == "treatment"
