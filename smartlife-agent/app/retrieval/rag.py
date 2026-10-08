"""
RAG 检索模块 - 语义检索（持久化 + 增量更新 + 删除）
"""
import os
import json
import hashlib
import shutil
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from langchain_chroma import Chroma
from app.config import create_embeddings, get_embedding_identity
from langchain_text_splitters import RecursiveCharacterTextSplitter
from app.document_import import SUPPORTED_DOCUMENT_SUFFIXES, load_document_records


class EmbeddingConfigurationChanged(RuntimeError):
    """现有向量库与当前 Embedding 配置不兼容。"""


@dataclass
class RetrievalResult:
    """统一的检索返回结构，明确区分成功、空结果和异常。"""

    documents: List[Dict[str, Any]] = field(default_factory=list)
    scores: List[float] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    source: str = "chroma"
    ok: bool = True
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "documents": self.documents,
            "scores": self.scores,
            "diagnostics": self.diagnostics,
            "source": self.source,
            "ok": self.ok,
            "error": self.error,
        }


class RAGRetriever:
    """RAG 检索器 - 支持持久化、增量更新、删除"""

    def __init__(self, data_dir: str = None, collection_name: str = "smartlife", persist_dir: str = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        if data_dir is None:
            data_dir = os.path.join(base_dir, "data")
        if persist_dir is None:
            persist_dir = os.path.join(base_dir, "chroma_db")

        # macOS 上 /tmp 常是 /private/tmp 的符号链接；统一真实路径避免来源删除失配。
        self.data_dir = os.path.realpath(data_dir)
        self.persist_dir = os.path.realpath(persist_dir)
        self.collection_name = collection_name

        self.embeddings = create_embeddings()
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=50,
            separators=["\n\n", "\n", "。", "！", "？", ".", " "]
        )

        self.vectorstore = None
        self.retriever = None
        self._initialize(collection_name)

    @property
    def _manifest_path(self) -> str:
        return os.path.join(self.persist_dir, "embedding_manifest.json")

    def _write_embedding_manifest(self, dimension: Optional[int] = None) -> None:
        os.makedirs(self.persist_dir, exist_ok=True)
        payload = {
            **get_embedding_identity(),
            "dimension": dimension,
            "chunk_size": getattr(getattr(self, "text_splitter", None), "_chunk_size", None),
            "chunk_overlap": getattr(
                getattr(self, "text_splitter", None), "_chunk_overlap", None
            ),
            "collection_name": self.collection_name,
        }
        with open(self._manifest_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def _validate_embedding_compatibility(self, collection) -> Optional[int]:
        identity = get_embedding_identity()
        dimension = None
        try:
            sample = collection.get(limit=1, include=["embeddings"])
            embeddings = sample.get("embeddings")
            if embeddings is not None and len(embeddings) > 0:
                dimension = len(embeddings[0])
        except Exception:
            dimension = None

        if os.path.exists(self._manifest_path):
            try:
                with open(self._manifest_path, "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                raise EmbeddingConfigurationChanged(
                    f"向量库 Embedding 清单损坏：{exc}；请全量重建向量库"
                ) from exc
            previous_model = manifest.get("model", "")
            previous_provider = manifest.get("provider", "")
            previous_dimension = manifest.get("dimension")
            previous_chunk_size = manifest.get("chunk_size")
            previous_chunk_overlap = manifest.get("chunk_overlap")
            current_chunk_size = getattr(
                getattr(self, "text_splitter", None), "_chunk_size", None
            )
            current_chunk_overlap = getattr(
                getattr(self, "text_splitter", None), "_chunk_overlap", None
            )
            if previous_chunk_size and previous_chunk_size != current_chunk_size:
                raise EmbeddingConfigurationChanged(
                    "文本切片大小已变化。为保证索引一致性，请全量重建向量库。"
                )
            if (
                previous_chunk_overlap is not None
                and previous_chunk_overlap != current_chunk_overlap
            ):
                raise EmbeddingConfigurationChanged(
                    "文本切片重叠已变化。为保证索引一致性，请全量重建向量库。"
                )
            if previous_model and (
                previous_model != identity.get("model")
                or previous_provider != identity.get("provider")
            ):
                raise EmbeddingConfigurationChanged(
                    "Embedding 模型已变化：旧向量模型="
                    f"{previous_provider}/{previous_model}，当前="
                    f"{identity.get('provider')}/{identity.get('model')}。"
                    "不同模型的向量不能混用，请先执行 "
                    "`scripts/import_data.py vectors --rebuild` 全量重建。"
                )
            if previous_dimension and dimension and int(previous_dimension) != int(dimension):
                raise EmbeddingConfigurationChanged(
                    f"Embedding 向量维度不兼容：现有 {previous_dimension}，当前 {dimension}。"
                    "请执行 `scripts/import_data.py vectors --rebuild` 全量重建。"
                )
        self._write_embedding_manifest(dimension)
        return dimension

    def _initialize(self, collection_name: str):
        """初始化向量数据库 - 优先加载已有库，不存在则创建"""
        if os.path.exists(self.persist_dir) and os.listdir(self.persist_dir):
            # 已有持久化数据，直接加载（秒启动）
            print(f"[RAG] 加载已有向量库: {self.persist_dir}")
            self.vectorstore = Chroma(
                persist_directory=self.persist_dir,
                embedding_function=self.embeddings,
                collection_name=collection_name,
            )
            count = self.vectorstore._collection.count()
            if count and not os.path.exists(self._manifest_path):
                raise EmbeddingConfigurationChanged(
                    "现有向量库缺少 embedding_manifest.json，无法证明其 Embedding 模型。"
                    "请执行 `scripts/import_data.py vectors --rebuild` 全量重建。"
                )
            self._validate_embedding_compatibility(self.vectorstore._collection)
            print(f"[RAG] 已加载 {count} 条向量")
        else:
            # 首次启动，从文档创建
            print(f"[RAG] 首次启动，从文档创建向量库...")
            documents = self._load_documents()
            splits = self.text_splitter.split_documents(documents)
            print(f"[RAG] 文档分割完成: {len(documents)} 个文件 -> {len(splits)} 个片段")

            self.vectorstore = Chroma.from_documents(
                documents=splits,
                embedding=self.embeddings,
                collection_name=collection_name,
                persist_directory=self.persist_dir,
            )
            dimension = None
            if splits:
                try:
                    dimension = len(self.embeddings.embed_query(splits[0].page_content[:200]))
                except Exception:
                    dimension = None
            self._write_embedding_manifest(dimension)
            print(f"[RAG] 向量库创建完成: {len(splits)} 条向量已持久化")

        self.retriever = self.vectorstore.as_retriever(search_kwargs={"k": 20})

    def _load_records(
        self,
        file_path: str,
        source_type: str,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """加载单个文档并附加稳定元数据。"""
        normalized_path = os.path.realpath(file_path)
        records = load_document_records(normalized_path, source_type, extra_metadata)
        content_hash = hashlib.sha256(
            "\n\n".join(record.page_content for record in records).encode("utf-8")
        ).hexdigest()
        for record_index, record in enumerate(records):
            record.metadata.setdefault("record_index", record_index)
            record.metadata["content_hash"] = content_hash
        return records

    def _load_documents(self) -> list:
        """加载所有知识文档；首次建库支持 TXT、Markdown、JSONL、PDF、DOCX、Excel。"""
        from langchain_core.documents import Document

        documents = []
        source_directories = [
            (source_type, os.path.join(self.data_dir, source_type))
            for source_type in ("reviews", "guides", "activities")
        ]
        source_directories.extend(
            (source_type, os.path.join(self.data_dir, "imports", source_type))
            for source_type in ("reviews", "guides", "activities")
        )
        for source_type, dir_path in source_directories:
            if not os.path.exists(dir_path):
                continue
            for file_path in sorted(Path(dir_path).rglob("*")):
                if not file_path.is_file():
                    continue
                if file_path.suffix.lower() not in SUPPORTED_DOCUMENT_SUFFIXES:
                    continue
                documents.extend(self._load_records(str(file_path), source_type))

        if not documents:
            documents = [Document(
                page_content="SmartLife Agent 电商平台和旅游平台",
                metadata={"source_type": "default", "doc_id": "default"}
            )]
        return documents

    def add_documents(
        self,
        file_paths: List[str],
        source_type: str = "reviews",
        metadata: Optional[Dict[str, Any]] = None,
        *,
        id_namespace: Optional[str] = None,
        sync_generation: Optional[str] = None,
    ) -> Dict[str, Any]:
        """增量添加文档，只计算新文档的 Embedding。"""
        loaded_docs = []
        skipped = []
        for file_path in file_paths:
            if not os.path.exists(file_path):
                skipped.append({"file": file_path, "reason": "文件不存在"})
                continue
            try:
                extra = dict(metadata or {})
                if sync_generation:
                    extra["sync_generation"] = sync_generation
                loaded_docs.extend(self._load_records(file_path, source_type, extra))
            except Exception as exc:
                skipped.append({"file": file_path, "reason": f"{type(exc).__name__}: {exc}"})

        if not loaded_docs:
            return {
                "files": len(file_paths),
                "documents": 0,
                "chunks": 0,
                "skipped": skipped,
            }

        splits = self.text_splitter.split_documents(loaded_docs)
        ids = []
        namespace = id_namespace or "stable"
        for index, _doc in enumerate(splits):
            source = _doc.metadata.get("source", "")
            record_index = _doc.metadata.get("record_index", 0)
            content_hash = _doc.metadata.get("content_hash", "")
            identity = f"{namespace}:{source}:{record_index}:{content_hash}:{index}"
            ids.append(hashlib.sha1(identity.encode("utf-8")).hexdigest())
        self.vectorstore.add_documents(splits, ids=ids)
        return {
            "files": len(file_paths),
            "documents": len(loaded_docs),
            "chunks": len(splits),
            "skipped": skipped,
            "ids": ids,
        }

    def sync_file(
        self,
        file_path: str,
        source_type: str = "reviews",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """替换式同步一个文件：先删旧来源向量，再写入新切片。"""
        absolute_path = os.path.realpath(file_path)
        if not os.path.exists(absolute_path):
            raise FileNotFoundError(absolute_path)

        old_ids = self._source_ids(absolute_path)
        generation = uuid.uuid4().hex
        added = self.add_documents(
            [absolute_path],
            source_type=source_type,
            metadata=metadata,
            id_namespace=generation,
            sync_generation=generation,
        )
        new_ids = list(added.get("ids") or [])
        if added.get("skipped"):
            if new_ids:
                self.vectorstore._collection.delete(ids=new_ids)
            raise RuntimeError(f"文档导入失败: {added['skipped']}")

        deleted_count = 0
        if old_ids:
            try:
                self.vectorstore._collection.delete(ids=old_ids)
                deleted_count = len(old_ids)
            except Exception as exc:
                # 新旧 ID 使用不同 namespace，失败时可安全撤回新版本。
                self.vectorstore._collection.delete(ids=new_ids)
                raise RuntimeError(f"替换旧向量失败，已回滚新版本: {exc}") from exc
        return {
            "source": absolute_path,
            "deleted": {"deleted": deleted_count, "ids": old_ids},
            "added": added,
            "generation": generation,
            "atomic": True,
        }

    def _source_ids(self, source_path: str) -> List[str]:
        absolute_path = os.path.realpath(source_path)
        collection = self.vectorstore._collection
        results = collection.get(where={"source": absolute_path}, include=["metadatas"])
        return list(results.get("ids") or [])

    def delete_by_source(self, source_path: str) -> Dict[str, Any]:
        """按来源文件删除向量。"""
        absolute_path = os.path.realpath(source_path)
        try:
            collection = self.vectorstore._collection
            ids = self._source_ids(absolute_path)
            if ids:
                collection.delete(ids=ids)
            return {"source": absolute_path, "deleted": len(ids), "ids": ids}
        except Exception as exc:
            return {
                "source": absolute_path,
                "deleted": 0,
                "ids": [],
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }

    def delete_by_ids(self, doc_ids: List[str]):
        """按 ID 删除向量"""
        try:
            self.vectorstore._collection.delete(ids=doc_ids)
            print(f"[RAG] 已删除 {len(doc_ids)} 条向量")
        except Exception as e:
            print(f"[RAG] 删除失败: {e}")

    def get_collection_stats(self) -> Dict[str, Any]:
        """获取向量库统计信息"""
        collection = self.vectorstore._collection
        count = collection.count()
        return {
            "total_vectors": count,
            "collection_name": self.collection_name,
            "persist_dir": self.persist_dir,
        }

    def retrieve(
        self,
        query: str,
        k: int = 5,
        owner: Optional[str] = None,
    ) -> RetrievalResult:
        """统一检索 API，供 V2 主链路和其他调用方使用。"""
        if not isinstance(query, str) or not query.strip():
            return RetrievalResult(ok=False, error="query 不能为空", diagnostics=["RAG 查询为空"])
        if not isinstance(k, int) or k <= 0:
            return RetrievalResult(ok=False, error="k 必须为正整数", diagnostics=["RAG 参数 k 无效"])

        try:
            if self.vectorstore is None:
                raise RuntimeError("向量库未初始化")

            if owner:
                owner_filter = {"owner": {"$in": [owner, "public"]}}
                try:
                    # 优先让向量库按 owner/public 过滤，避免拉取大量他人文档后才截断。
                    raw_results = self.vectorstore.similarity_search_with_score(
                        query,
                        k=k,
                        filter=owner_filter,
                    )
                except TypeError:
                    # 兼容只接受 query/k 的旧向量库或测试替身。
                    fetch_k = max(k * 8, 40)
                    raw_results = self.vectorstore.similarity_search_with_score(
                        query,
                        k=fetch_k,
                    )
            else:
                raw_results = self.vectorstore.similarity_search_with_score(query, k=k)
            if owner:
                # 无论向量库是否支持 filter，都在返回前再次做权限隔离。
                raw_results = [
                    (document, score)
                    for document, score in raw_results
                    if (document.metadata or {}).get("owner", "public") in {owner, "public"}
                ][:k]
            documents: List[Dict[str, Any]] = []
            scores: List[float] = []
            for doc, score in raw_results:
                documents.append({
                    "content": doc.page_content,
                    "metadata": dict(doc.metadata or {}),
                    "score": float(score),
                })
                scores.append(float(score))

            diagnostics = [] if documents else ["RAG 未返回文档"]
            return RetrievalResult(
                documents=documents,
                scores=scores,
                diagnostics=diagnostics,
                source="chroma",
                ok=True,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            return RetrievalResult(
                documents=[],
                scores=[],
                diagnostics=[f"RAG 检索失败: {message}"],
                source="chroma",
                ok=False,
                error=message,
            )

    def search(
        self,
        query: str,
        k: int = 20,
        owner: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """兼容旧接口：返回文档字典列表。"""
        return self.retrieve(query, k=k, owner=owner).documents

    def search_with_score(
        self,
        query: str,
        k: int = 20,
        owner: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """兼容旧接口：返回带分数的文档字典列表。"""
        return self.retrieve(query, k=k, owner=owner).documents


def rebuild_vector_store(
    data_dir: Optional[str] = None,
    persist_dir: Optional[str] = None,
    collection_name: str = "smartlife",
) -> Dict[str, Any]:
    """删除旧向量目录并从知识文档全量重建；不会修改 SQLite 或原始文档。"""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    resolved_data = os.path.realpath(data_dir or os.path.join(base_dir, "data"))
    resolved_persist = os.path.realpath(persist_dir or os.path.join(base_dir, "chroma_db"))
    staging = resolved_persist + f".rebuild-{uuid.uuid4().hex[:10]}"
    backup = None
    try:
        rag = RAGRetriever(
            data_dir=resolved_data,
            collection_name=collection_name,
            persist_dir=staging,
        )
        stats = rag.get_collection_stats()
        stats["persist_dir"] = resolved_persist
        if os.path.exists(resolved_persist):
            backup = resolved_persist + ".backup"
            if os.path.exists(backup):
                backup += f"-{uuid.uuid4().hex[:10]}"
            os.replace(resolved_persist, backup)
        try:
            os.replace(staging, resolved_persist)
        except Exception:
            if backup and not os.path.exists(resolved_persist):
                os.replace(backup, resolved_persist)
            raise
    except Exception:
        if os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "rebuilt": True,
        "data_dir": resolved_data,
        "persist_dir": resolved_persist,
        "backup_dir": backup,
        "collection": stats,
    }
