"""
Reranker 模块 - Cross-Encoder 精排
"""
from typing import List, Dict, Any

class CrossEncoderReranker:
    """Cross-Encoder 精排器"""
    
    def __init__(self, top_n: int = 5):
        self.top_n = top_n
        self.model = None
        self._initialize()
    
    def _initialize(self):
        """初始化 Cross-Encoder 模型"""
        try:
            from sentence_transformers import CrossEncoder
            self.model = CrossEncoder("BAAI/bge-reranker-base")
        except Exception as e:
            print(f"Warning: Cross-Encoder 初始化失败: {e}，将使用 LLM 模拟 rerank")
            self.model = None
    
    def rerank(self, query: str, documents: List[Dict[str, Any]], top_n: int = None) -> List[Dict[str, Any]]:
        """精排"""
        if not documents:
            return []
        
        if top_n is None:
            top_n = self.top_n
        
        if self.model is not None:
            # 使用 Cross-Encoder
            pairs = [(query, doc["content"]) for doc in documents]
            scores = self.model.predict(pairs)
            
            for i, score in enumerate(scores):
                documents[i]["rerank_score"] = float(score)
            
            documents.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        else:
            # 降级：使用关键词匹配模拟
            query_lower = query.lower()
            for doc in documents:
                content_lower = doc["content"].lower()
                keyword_hits = sum(1 for word in query_lower.split() if word in content_lower)
                doc["rerank_score"] = keyword_hits / max(len(query_lower.split()), 1)
            documents.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        
        return documents[:top_n]
