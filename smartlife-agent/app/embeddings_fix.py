"""
修复: 使用 DashScope 原生 Embedding API 格式
"""
import requests
import json
from typing import List


class DashScopeEmbeddings:
    """调用阿里云 DashScope Embedding API"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.model = model
        # 使用 DashScope 原生 API 端点
        self.url = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"

    def _call_api(self, texts: List[str]) -> List[List[float]]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # DashScope 原生格式: input.texts
        payload = {
            "model": self.model,
            "input": {"texts": texts},
        }
        try:
            resp = requests.post(self.url, headers=headers, json=payload, timeout=60)
        except Exception as e:
            print(f"[RAG-ERROR] Embedding API 请求失败: {type(e).__name__}: {e}")
            raise
        if resp.status_code != 200:
            print(f"[RAG-ERROR] Embedding API 返回非200: {resp.status_code} {resp.text[:300]}")
            raise Exception(f"Embedding API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        embeddings = [item["embedding"] for item in data["output"]["embeddings"]]
        return embeddings

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # DashScope 限制每次最多 10 条，需要分批请求
        all_embeddings = []
        batch_size = 10
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            all_embeddings.extend(self._call_api(batch))
        return all_embeddings

    def embed_query(self, text: str) -> List[float]:
        return self._call_api([text])[0]
