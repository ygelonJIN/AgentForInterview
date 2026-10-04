"""
摘要压缩器 - 对话摘要生成
"""
import json
from typing import List, Dict, Any
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_small_llm


class MemoryCompressor:
    """记忆压缩器"""

    def __init__(self, model_name: str = None):
        self.llm = create_small_llm(max_tokens=500)

        self.summary_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """你是一个对话摘要专家。将以下对话压缩为简洁的摘要。

摘要要求：
1. 保留关键信息：用户需求、偏好、重要决策
2. 保留具体数据：商品名称、价格、地点、日期
3. 用中文输出
4. 长度控制在200字以内
5. 按以下结构组织：
   - 用户需求：...
   - 关键偏好：...
   - 重要决策：...""",
                ),
                ("user", "{conversation}"),
            ]
        )

        self.chain = self.summary_prompt | self.llm

    def compress(self, messages: List[Dict[str, str]]) -> str:
        """压缩对话为摘要"""
        if not messages:
            return ""

        conversation = "\n".join(
            [f"{msg['role']}: {msg['content']}" for msg in messages]
        )

        result = self.chain.invoke({"conversation": conversation})
        return result.content

    def extract_preferences(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """从对话中提取用户偏好"""
        pref_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """从以下对话中提取用户偏好，以JSON格式返回：
{
    "shopping_preferences": {"categories": [], "price_range": "", "brands": [], "style": ""},
    "travel_preferences": {"destinations": [], "budget": "", "interests": [], "travel_style": ""},
    "general": {"language": "", "communication_style": ""}
}""",
                ),
                ("user", "{conversation}"),
            ]
        )

        conversation = "\n".join(
            [f"{msg['role']}: {msg['content']}" for msg in messages]
        )
        chain = pref_prompt | self.llm
        result = chain.invoke({"conversation": conversation})

        try:
            return json.loads(result.content)
        except (json.JSONDecodeError, ValueError):
            return {"raw": result.content}
