"""P0 安全、会话隔离和 RAG 主链路回归测试。"""

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.memory.short_term import ShortTermMemory
from app.retrieval.rag import RetrievalResult, RAGRetriever
from app.session import build_thread_id


def test_config_separates_api_keys_into_local_secrets_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", str(config_file))
    monkeypatch.setattr(config, "_main", {"api_key": "runtime-secret", "base_url": "https://main", "model": "main-model"})
    monkeypatch.setattr(config, "_small", {"api_key": "", "base_url": "", "model": ""})
    monkeypatch.setattr(config, "_embedding", {"api_key": "", "base_url": "", "model": ""})

    config._save_config()

    raw = config_file.read_text(encoding="utf-8")
    payload = json.loads(raw)
    secrets_file = tmp_path / "config.secrets.json"
    secrets_raw = secrets_file.read_text(encoding="utf-8")
    assert "runtime-secret" not in raw
    assert "api_key" not in payload["main"]
    assert "runtime-secret" in secrets_raw
    assert secrets_file.stat().st_mode & 0o777 == 0o600

    config_file.write_text(json.dumps({
        "main": {"api_key": "legacy-secret", "base_url": "https://legacy", "model": "legacy-model"},
        "small": {"api_key": "legacy-small", "base_url": "", "model": ""},
        "embedding": {"api_key": "legacy-embedding", "base_url": "", "model": ""},
    }), encoding="utf-8")
    monkeypatch.setattr(config, "_main", {"api_key": "", "base_url": "", "model": ""})
    monkeypatch.setattr(config, "_small", {"api_key": "", "base_url": "", "model": ""})
    monkeypatch.setattr(config, "_embedding", {"api_key": "", "base_url": "", "model": ""})

    config._load_config()

    assert config._main == {"api_key": "legacy-secret", "base_url": "https://legacy", "model": "legacy-model"}
    assert config._small["api_key"] == "legacy-small"
    assert config._embedding["api_key"] == "legacy-embedding"
    migrated_config = json.loads(config_file.read_text(encoding="utf-8"))
    assert "api_key" not in migrated_config["main"]
    migrated_secrets = json.loads((tmp_path / "config.secrets.json").read_text(encoding="utf-8"))
    assert migrated_secrets["main"]["api_key"] == "legacy-secret"


def test_environment_main_key_falls_back_to_embedding(monkeypatch):
    monkeypatch.setattr(config, "_main", {"api_key": "", "base_url": "", "model": ""})
    monkeypatch.setattr(config, "_small", {"api_key": "", "base_url": "", "model": ""})
    monkeypatch.setattr(config, "_embedding", {"api_key": "", "base_url": "", "model": ""})
    monkeypatch.delenv("SMARTLIFE_MAIN_API_KEY", raising=False)
    monkeypatch.delenv("SMARTLIFE_SMALL_API_KEY", raising=False)
    monkeypatch.delenv("SMARTLIFE_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert config.has_api_key("main") is False
    monkeypatch.setenv("SMARTLIFE_MAIN_API_KEY", "env-main-key")

    assert config.has_api_key("main") is True
    assert config._get_effective_embedding()["api_key"] == "env-main-key"


def test_thread_ids_isolate_users_scenes_and_browser_sessions():
    shopping = build_thread_id("user-a", "shopping", "browser-1")
    travel = build_thread_id("user-a", "travel", "browser-1")
    other_browser = build_thread_id("user-a", "shopping", "browser-2")
    other_user = build_thread_id("user-b", "shopping", "browser-1")

    assert len({shopping, travel, other_browser, other_user}) == 4
    assert build_thread_id("a:b", "c", "d") != build_thread_id("a", "b:c", "d")

    with pytest.raises(ValueError):
        build_thread_id("", "shopping", "browser-1")


def test_short_term_memory_is_scoped_by_thread_id():
    memory = ShortTermMemory(max_messages=2)
    first = build_thread_id("user-a", "shopping", "browser-1")
    second = build_thread_id("user-b", "travel", "browser-2")

    memory.add_message(first, "user", "A1")
    memory.add_message(second, "user", "B1")
    memory.add_message(first, "assistant", "A2")
    memory.add_message(first, "user", "A3")

    assert [item["content"] for item in memory.get_history(first)] == ["A2", "A3"]
    assert [item["content"] for item in memory.get_history(second)] == ["B1"]


class _FakeVectorStore:
    def similarity_search_with_score(self, query, k):
        assert query == "跑步鞋评价"
        assert k == 2
        return [
            (SimpleNamespace(page_content="真实评价一", metadata={"source_type": "reviews"}), 0.1),
            (SimpleNamespace(page_content="真实评价二", metadata={"source_type": "reviews"}), 0.2),
        ]


def test_rag_retrieve_returns_structured_documents_and_explicit_failure():
    retriever = RAGRetriever.__new__(RAGRetriever)
    retriever.vectorstore = _FakeVectorStore()

    result = retriever.retrieve("跑步鞋评价", k=2)

    assert isinstance(result, RetrievalResult)
    assert result.ok is True
    assert result.source == "chroma"
    assert [doc["content"] for doc in result.documents] == ["真实评价一", "真实评价二"]
    assert result.scores == [0.1, 0.2]

    broken = RAGRetriever.__new__(RAGRetriever)
    broken.vectorstore = None
    failed = broken.retrieve("跑步鞋评价", k=2)

    assert failed.ok is False
    assert failed.documents == []
    assert failed.error
    assert failed.diagnostics


def test_rag_retrieve_uses_real_document_content():
    from langchain_chroma import Chroma
    from langchain_core.documents import Document
    from langchain_core.embeddings import Embeddings

    class HashedEmbeddings(Embeddings):
        def _embed(self, text):
            vector = [0.0] * 16
            for char in text:
                vector[ord(char) % len(vector)] += 1.0
            norm = sum(value * value for value in vector) ** 0.5 or 1.0
            return [value / norm for value in vector]

        def embed_documents(self, texts):
            return [self._embed(text) for text in texts]

        def embed_query(self, text):
            return self._embed(text)

    source = Path(__file__).parents[1] / "data" / "reviews" / "running_shoes_reviews.txt"
    content = source.read_text(encoding="utf-8")[:600]
    store = Chroma.from_documents(
        [Document(page_content=content, metadata={"source": str(source), "source_type": "reviews"})],
        HashedEmbeddings(),
        collection_name=f"p0-real-doc-{uuid.uuid4().hex}",
    )
    retriever = RAGRetriever.__new__(RAGRetriever)
    retriever.vectorstore = store

    result = retriever.retrieve("跑步鞋 缓震 舒适 评价", k=1)

    assert result.ok is True
    assert result.documents
    assert result.documents[0]["content"] == content
    assert result.documents[0]["metadata"]["source_type"] == "reviews"


class _FakeQueue:
    def __init__(self):
        self.calls = []

    async def emit_tool_call(self, name, detail, step=None):
        self.calls.append((name, detail, step))


class _FakeRag:
    def __init__(self, result):
        self.result = result

    def retrieve(self, query, k):
        return self.result


class _FakeSql:
    def query(self, query):
        return {
            "sql": "SELECT * FROM products",
            "explanation": "查找跑鞋",
            "needs_rag": ["评价"],
            "results": [{
                "id": 1,
                "name": "真实跑鞋",
                "price": 399,
                "brand": "TestBrand",
                "description": "真实商品描述",
            }],
            "count": 1,
        }


def test_v2_rag_only_path_uses_retrieve_result():
    from app.agents.orchestrator_v2 import OrchestratorV2

    orchestrator = OrchestratorV2.__new__(OrchestratorV2)
    orchestrator._rag_retriever = _FakeRag(RetrievalResult(
        documents=[{"content": "真实评价", "metadata": {}, "score": 0.1}],
        scores=[0.1],
    ))
    queue = _FakeQueue()

    result = asyncio.run(orchestrator._smart_retrieve(
        "跑步鞋评价",
        "rag_only",
        queue,
        needs={"needs_review": True, "needs_product": False},
    ))

    assert result["rag_ok"] is True
    assert "真实评价" in result["review_context"]
    assert queue.calls == [("rag", "返回 1 条文档", "retrieval")]


def test_v2_mixed_path_contains_sql_products_and_rag_documents():
    from app.agents.orchestrator_v2 import OrchestratorV2

    orchestrator = OrchestratorV2.__new__(OrchestratorV2)
    orchestrator._nl2sql_chain = _FakeSql()
    orchestrator._rag_retriever = _FakeRag(RetrievalResult(
        documents=[{"content": "真实评价", "metadata": {}, "score": 0.1}],
        scores=[0.1],
    ))
    queue = _FakeQueue()

    result = asyncio.run(orchestrator._smart_retrieve(
        "推荐跑鞋并告诉我评价",
        "mixed",
        queue,
        needs={"needs_review": True, "needs_product": True},
    ))

    assert result["strategy"] == "mixed"
    assert "真实跑鞋" in result["product_context"]
    assert "真实评价" in result["review_context"]
    assert [call[0] for call in queue.calls] == ["nl2sql", "rag"]


def test_v2_marks_rag_failure_instead_of_presenting_fake_reviews():
    from app.agents.orchestrator_v2 import OrchestratorV2

    orchestrator = OrchestratorV2.__new__(OrchestratorV2)
    orchestrator._rag_retriever = _FakeRag(RetrievalResult(
        ok=False,
        error="vector store missing",
        diagnostics=["RAG 检索失败: vector store missing"],
    ))

    result = asyncio.run(orchestrator._smart_retrieve(
        "跑步鞋评价",
        "rag_only",
        _FakeQueue(),
        needs={"needs_review": True, "needs_product": False},
    ))

    assert result["rag_ok"] is False
    assert result["rag_error"] == "vector store missing"
    assert result["review_context"] == ""
    assert "RAG 检索失败" in result["diagnostics"][0]
