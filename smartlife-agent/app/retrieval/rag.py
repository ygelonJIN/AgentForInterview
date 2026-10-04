"""
RAG 检索模块 - 语义检索（持久化 + 增量更新 + 删除）
"""
import os
import hashlib
from typing import List, Dict, Any, Optional
from langchain_chroma import Chroma
from app.config import create_embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import DirectoryLoader, TextLoader


class RAGRetriever:
    """RAG 检索器 - 支持持久化、增量更新、删除"""

    def __init__(self, data_dir: str = None, collection_name: str = "smartlife", persist_dir: str = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        if data_dir is None:
            data_dir = os.path.join(base_dir, "data")
        if persist_dir is None:
            persist_dir = os.path.join(base_dir, "chroma_db")

        self.data_dir = data_dir
        self.persist_dir = persist_dir
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
            print(f"[RAG] 向量库创建完成: {len(splits)} 条向量已持久化")

        self.retriever = self.vectorstore.as_retriever(search_kwargs={"k": 20})

    def _load_documents(self) -> list:
        """加载所有文档"""
        from langchain_core.documents import Document
        documents = []
        for subdir in ["reviews", "guides", "activities"]:
            dir_path = os.path.join(self.data_dir, subdir)
            if os.path.exists(dir_path):
                loader = DirectoryLoader(
                    dir_path, glob="*.txt", loader_cls=TextLoader,
                    loader_kwargs={"encoding": "utf-8"}
                )
                docs = loader.load()
                for doc in docs:
                    doc.metadata["source_type"] = subdir
                    doc.metadata["doc_id"] = hashlib.md5(
                        (doc.metadata.get("source", "") + doc.page_content[:100]).encode()
                    ).hexdigest()
                documents.extend(docs)

        if not documents:
            documents = [Document(
                page_content="SmartLife Agent 电商平台和旅游平台",
                metadata={"source_type": "default", "doc_id": "default"}
            )]
        return documents

    def add_documents(self, file_paths: List[str], source_type: str = "reviews"):
        """增量添加文档 - 只计算新文档的 embedding"""
        from langchain_core.documents import Document
        new_docs = []
        for fp in file_paths:
            if not os.path.exists(fp):
                continue
            loader = TextLoader(fp, encoding="utf-8")
            docs = loader.load()
            for doc in docs:
                doc.metadata["source_type"] = source_type
                doc.metadata["doc_id"] = hashlib.md5(
                    (fp + doc.page_content[:100]).encode()
                ).hexdigest()
            new_docs.extend(docs)

        if not new_docs:
            print("[RAG] 没有新文档需要添加")
            return

        splits = self.text_splitter.split_documents(new_docs)
        self.vectorstore.add_documents(splits)
        print(f"[RAG] 增量添加完成: {len(splits)} 条新向量")

    def delete_by_source(self, source_path: str):
        """按来源文件删除向量"""
        try:
            collection = self.vectorstore._collection
            # 获取所有文档的 metadata
            results = collection.get(where={"source": source_path}, include=["metadatas"])
            if results["ids"]:
                collection.delete(ids=results["ids"])
                print(f"[RAG] 已删除 {len(results['ids'])} 条向量 (source: {source_path})")
            else:
                print(f"[RAG] 未找到匹配的向量 (source: {source_path})")
        except Exception as e:
            print(f"[RAG] 删除失败: {e}")

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

    def search(self, query: str, k: int = 20) -> List[Dict[str, Any]]:
        """粗排检索"""
        docs = self.retriever.invoke(query)
        return [{"content": doc.page_content, "metadata": doc.metadata, "score": 0.0} for doc in docs[:k]]

    def search_with_score(self, query: str, k: int = 20) -> List[Dict[str, Any]]:
        """带分数的检索"""
        results = self.vectorstore.similarity_search_with_score(query, k=k)
        return [{"content": doc.page_content, "metadata": doc.metadata, "score": score} for doc, score in results]
