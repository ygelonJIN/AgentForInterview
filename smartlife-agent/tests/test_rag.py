"""
RAG 评测测试
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key-for-testing")

def test_reranker_class():
    """测试 Reranker 类定义"""
    from app.retrieval.reranker import CrossEncoderReranker
    assert hasattr(CrossEncoderReranker, 'rerank')
    assert hasattr(CrossEncoderReranker, '_initialize')

def test_reranker_keyword_fallback():
    """测试 Reranker 关键词降级（不下载模型）"""
    from app.retrieval.reranker import CrossEncoderReranker
    reranker = CrossEncoderReranker.__new__(CrossEncoderReranker)
    reranker.top_n = 2
    reranker.model = None  # 强制降级模式
    docs = [
        {"content": "这双鞋防水性能很好跑步舒适", "metadata": {}},
        {"content": "这个手机拍照很清晰", "metadata": {}},
        {"content": "跑步鞋缓震效果不错", "metadata": {}},
    ]
    result = reranker.rerank("跑步鞋", docs, top_n=2)
    assert len(result) == 2
    assert all("rerank_score" in r for r in result)

def test_nl2sql_model():
    """测试 NL2SQL 输出模型"""
    from app.retrieval.nl2sql import SQLQuery
    query = SQLQuery(sql="SELECT * FROM products WHERE price < 500", explanation="查询500以下", needs_rag=["耐磨"])
    assert query.sql.startswith("SELECT")
    assert "耐磨" in query.needs_rag

def test_rag_retriever_class():
    """测试 RAG 检索器类定义"""
    from app.retrieval.rag import RAGRetriever
    assert hasattr(RAGRetriever, 'retrieve')
    assert hasattr(RAGRetriever, 'search')
    assert hasattr(RAGRetriever, 'search_with_score')

def test_fusion_class():
    """测试混合检索器类定义"""
    from app.retrieval.fusion import HybridRetriever
    assert hasattr(HybridRetriever, 'search')
