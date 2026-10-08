import pytest

from app.retrieval.rag import RAGRetriever


class FakeCollection:
    def __init__(self):
        self.records = {}

    def get(self, where=None, include=None):
        source = (where or {}).get("source")
        ids = [item_id for item_id, meta in self.records.items() if meta.get("source") == source]
        return {"ids": ids, "metadatas": [self.records[item_id] for item_id in ids]}

    def delete(self, ids):
        for item_id in ids:
            self.records.pop(item_id, None)


class FakeVectorStore:
    def __init__(self):
        self._collection = FakeCollection()

    def add_documents(self, documents, ids=None):
        ids = ids or [str(index) for index in range(len(documents))]
        for item_id, document in zip(ids, documents):
            self._collection.records[item_id] = dict(document.metadata)
        return ids


def test_jsonl_records_preserve_product_metadata(tmp_path):
    path = tmp_path / "reviews.jsonl"
    path.write_text(
        '{"content":"防水效果很好","product_id":31,"user_id":"user_001","rating":5}\n',
        encoding="utf-8",
    )
    rag = RAGRetriever.__new__(RAGRetriever)

    records = rag._load_records(str(path), "reviews", {"tenant": "demo"})

    assert records[0].page_content == "防水效果很好"
    assert records[0].metadata["product_id"] == 31
    assert records[0].metadata["tenant"] == "demo"
    assert records[0].metadata["source"] == str(path.resolve())


def test_sync_file_replaces_existing_vectors(tmp_path):
    path = tmp_path / "reviews.jsonl"
    path.write_text('{"content":"第一版","product_id":31}\n', encoding="utf-8")
    rag = RAGRetriever.__new__(RAGRetriever)
    rag.vectorstore = FakeVectorStore()
    rag.text_splitter = type("Splitter", (), {
        "split_documents": staticmethod(lambda documents: documents),
    })()

    rag.sync_file(str(path), "reviews")
    assert len(rag.vectorstore._collection.records) == 1

    path.write_text('{"content":"第二版","product_id":31}\n', encoding="utf-8")
    result = rag.sync_file(str(path), "reviews")

    assert result["deleted"]["deleted"] == 1
    assert result["added"]["chunks"] == 1
    assert len(rag.vectorstore._collection.records) == 1
    metadata = next(iter(rag.vectorstore._collection.records.values()))
    assert metadata["product_id"] == 31


def test_embedding_model_change_requires_full_rebuild(tmp_path, monkeypatch):
    import json

    from app.retrieval.rag import EmbeddingConfigurationChanged, RAGRetriever

    class EmbeddingCollection:
        def get(self, limit=None, include=None):
            return {"embeddings": [[0.1, 0.2, 0.3]]}

    rag = RAGRetriever.__new__(RAGRetriever)
    rag.persist_dir = str(tmp_path)
    rag.collection_name = "smartlife"
    (tmp_path / "embedding_manifest.json").write_text(
        json.dumps({
            "provider": "dashscope",
            "model": "text-embedding-v1",
            "dimension": 3,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.retrieval.rag.get_embedding_identity",
        lambda: {"provider": "dashscope", "model": "text-embedding-v2", "base_url": ""},
    )

    with pytest.raises(EmbeddingConfigurationChanged, match="全量重建"):
        rag._validate_embedding_compatibility(EmbeddingCollection())


def test_rebuild_source_discovery_includes_ui_imports(tmp_path):
    reviews = tmp_path / "reviews"
    uploads = tmp_path / "imports" / "reviews"
    reviews.mkdir()
    uploads.mkdir(parents=True)
    (reviews / "seed.txt").write_text("原始评价", encoding="utf-8")
    (uploads / "uploaded.txt").write_text("网页上传评价", encoding="utf-8")

    rag = RAGRetriever.__new__(RAGRetriever)
    rag.data_dir = str(tmp_path)
    documents = rag._load_documents()

    by_content = {document.page_content: document for document in documents}
    assert by_content["原始评价"].metadata["source_type"] == "reviews"
    assert by_content["网页上传评价"].metadata["source_type"] == "reviews"


def test_sync_failure_before_replace_preserves_old_vectors(tmp_path):
    path = tmp_path / "reviews.jsonl"
    path.write_text('{"content":"旧版本","product_id":31}\n', encoding="utf-8")
    rag = RAGRetriever.__new__(RAGRetriever)
    rag.vectorstore = FakeVectorStore()
    rag.vectorstore._collection.records["old-id"] = {"source": str(path.resolve())}

    def failed_add(*args, **kwargs):
        return {"chunks": 0, "ids": [], "skipped": [{"reason": "Embedding failed"}]}

    rag.add_documents = failed_add
    with pytest.raises(RuntimeError, match="文档导入失败"):
        rag.sync_file(str(path), "reviews")

    assert "old-id" in rag.vectorstore._collection.records


def test_invalid_jsonl_dry_run_style_loader_rejects_content(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"broken":\n', encoding="utf-8")
    rag = RAGRetriever.__new__(RAGRetriever)

    with pytest.raises(Exception, match="JSONL"):
        rag._load_records(str(path), "reviews")


def test_sync_delete_failure_rolls_back_new_generation(tmp_path):
    path = tmp_path / "reviews.jsonl"
    path.write_text('{"content":"新版本","product_id":31}\n', encoding="utf-8")

    class FailOldDelete:
        def __init__(self):
            self.records = {
                "old-id": {"source": str(path.resolve())},
                "new-id": {"source": str(path.resolve())},
            }

        def get(self, where=None, include=None):
            source = (where or {}).get("source")
            ids = [
                item_id for item_id, metadata in self.records.items()
                if metadata.get("source") == source
            ]
            return {"ids": ids, "metadatas": [self.records[item_id] for item_id in ids]}

        def delete(self, ids):
            for item_id in ids:
                if item_id == "old-id":
                    raise RuntimeError("delete old failed")
                self.records.pop(item_id, None)

    rag = RAGRetriever.__new__(RAGRetriever)
    rag.vectorstore = FakeVectorStore()
    rag.vectorstore._collection = FailOldDelete()
    rag.add_documents = lambda *args, **kwargs: {
        "chunks": 1,
        "ids": ["new-id"],
        "skipped": [],
    }

    with pytest.raises(RuntimeError, match="已回滚新版本"):
        rag.sync_file(str(path), "reviews")

    assert "old-id" in rag.vectorstore._collection.records
    assert "new-id" not in rag.vectorstore._collection.records


def test_owner_filter_hides_private_documents_but_keeps_public(tmp_path):
    from langchain_core.documents import Document

    class OwnerAwareVectorStore:
        def similarity_search_with_score(self, query, k):
            return [
                (Document(page_content="私人文档", metadata={"owner": "user-a"}), 0.1),
                (Document(page_content="公共文档", metadata={"owner": "public"}), 0.2),
                (Document(page_content="他人文档", metadata={"owner": "user-b"}), 0.3),
            ][:k]

    rag = RAGRetriever.__new__(RAGRetriever)
    rag.vectorstore = OwnerAwareVectorStore()

    mine = rag.retrieve("查询", k=5, owner="user-a")
    assert {item["content"] for item in mine.documents} == {"私人文档", "公共文档"}

    other = rag.retrieve("查询", k=5, owner="user-b")
    assert {item["content"] for item in other.documents} == {"公共文档", "他人文档"}


def test_owner_filter_is_pushed_down_to_vector_store_when_supported(tmp_path):
    from langchain_core.documents import Document

    class FilterAwareVectorStore:
        def __init__(self):
            self.calls = []

        def similarity_search_with_score(self, query, k, filter=None):
            self.calls.append((query, k, filter))
            return [
                (Document(page_content="我的文档", metadata={"owner": "user-a"}), 0.1),
                (Document(page_content="公共文档", metadata={"owner": "public"}), 0.2),
            ][:k]

    rag = RAGRetriever.__new__(RAGRetriever)
    rag.vectorstore = FilterAwareVectorStore()

    result = rag.retrieve("查询", k=5, owner="user-a")

    assert result.ok is True
    assert rag.vectorstore.calls == [(
        "查询",
        5,
        {"owner": {"$in": ["user-a", "public"]}},
    )]
    assert {item["content"] for item in result.documents} == {"我的文档", "公共文档"}


def test_embedding_manifest_records_numpy_vector_dimension(tmp_path, monkeypatch):
    import json

    import numpy as np

    from app.retrieval.rag import RAGRetriever

    class NumpyCollection:
        def get(self, limit=None, include=None):
            return {"embeddings": np.array([[0.1, 0.2, 0.3, 0.4]])}

    rag = RAGRetriever.__new__(RAGRetriever)
    rag.persist_dir = str(tmp_path)
    rag.collection_name = "smartlife"
    monkeypatch.setattr(
        "app.retrieval.rag.get_embedding_identity",
        lambda: {"provider": "test", "model": "embedding-test", "base_url": ""},
    )

    rag._validate_embedding_compatibility(NumpyCollection())

    manifest = json.loads((tmp_path / "embedding_manifest.json").read_text(encoding="utf-8"))
    assert manifest["dimension"] == 4
