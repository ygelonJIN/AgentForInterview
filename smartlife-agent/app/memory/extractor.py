"""
记忆提取器 - 从对话中提取候选记忆

原则：
- 只提取事实，不推断偏好
- 用户手动确认后才写入
- 允许用户编辑/删除
"""
import json
import re
from typing import List, Dict, Any, Optional
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_small_llm


class MemoryExtractor:
    """记忆提取器"""

    def __init__(self):
        self.llm = create_small_llm(max_tokens=1500)
        
        # 简化提示词，避免 JSON 格式问题
        self.extract_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个记忆提取专家。从对话中提取有价值的事实信息。

提取原则：
1. 只提取明确提到的事实，不推断隐含信息
2. 不判断用户的偏好
3. 提取的信息应该是客观的、可验证的

请提取以下类别的信息：
- 购物相关：商品名称、价格、数量、规格
- 旅行相关：目的地、日期、人数、预算
- 其他重要信息

请以 JSON 数组格式返回，每个元素包含：
- content: 事实描述
- category: shopping/travel/general
- confidence: high/medium/low

重要：必须只返回一个 JSON 数组，不要有任何其他文字。如果没有值得提取的信息，返回空数组 []"""),
            ("user", "{conversation}")
        ])
        
        self.chain = self.extract_prompt | self.llm

    def extract_candidates(self, messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        从对话中提取候选记忆
        
        Args:
            messages: 消息列表，格式 [{"role": "user/assistant", "content": "..."}]
            
        Returns:
            候选记忆列表
        """
        if not messages:
            return []
        
        # 格式化对话
        conversation = "\n".join([
            f"{msg['role']}: {msg['content']}" 
            for msg in messages
        ])
        
        try:
            result = self.chain.invoke({"conversation": conversation})
            content = result.content.strip()
            
            # 尝试解析 JSON
            candidates = self._parse_json(content)
            
            # 验证格式
            if not isinstance(candidates, list):
                return []
            
            # 过滤无效条目
            valid_candidates = []
            for c in candidates:
                if isinstance(c, dict) and "content" in c:
                    valid_candidates.append({
                        "content": c["content"],
                        "category": c.get("category", "general"),
                        "confidence": c.get("confidence", "medium")
                    })
            
            return valid_candidates
            
        except Exception as e:
            print(f"Warning: 记忆提取失败: {e}")
            return []

    def _parse_json(self, content: str) -> List[Dict[str, Any]]:
        """
        解析 JSON，处理各种格式问题
        """
        # 如果内容为空，返回空列表
        if not content:
            return []
        
        # 尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        
        # 尝试提取 JSON 数组
        try:
            # 查找 JSON 数组
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                return json.loads(match.group())
        except json.JSONDecodeError:
            pass
        
        # 尝试提取单个 JSON 对象
        try:
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return [json.loads(match.group())]
        except json.JSONDecodeError:
            pass
        
        # 如果都失败了，尝试手动解析
        try:
            # 尝试修复常见的 JSON 格式问题
            content = content.replace("'", '"')  # 单引号替换为双引号
            content = re.sub(r',\s*}', '}', content)  # 移除对象末尾的逗号
            content = re.sub(r',\s*]', ']', content)  # 移除数组末尾的逗号
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        
        # 如果所有方法都失败，返回空列表
        return []

    def format_for_display(self, candidates: List[Dict[str, Any]]) -> str:
        """
        格式化候选记忆用于显示
        
        Args:
            candidates: 候选记忆列表
            
        Returns:
            格式化的字符串
        """
        if not candidates:
            return ""
        
        lines = ["**检测到以下信息，是否保存？**\n"]
        for i, c in enumerate(candidates, 1):
            emoji = {"shopping": "🛍️", "travel": "✈️", "general": "📝"}.get(c["category"], "📝")
            confidence = {"high": "高", "medium": "中", "low": "低"}.get(c["confidence"], "中")
            lines.append(f"{i}. {emoji} {c['content']} (置信度: {confidence})")
        
        lines.append("\n请回复要保存的编号（如 1,3），或回复「跳过」不保存。")
        return "\n".join(lines)

    def parse_user_selection(self, user_input: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        解析用户选择
        
        Args:
            user_input: 用户输入（如 "1,3" 或 "跳过"）
            candidates: 候选记忆列表
            
        Returns:
            用户选择的记忆列表
        """
        user_input = user_input.strip().lower()
        
        if user_input in ["跳过", "skip", "no", "不保存"]:
            return []
        
        # 解析编号
        try:
            indices = [int(x.strip()) - 1 for x in user_input.split(",") if x.strip().isdigit()]
            selected = [candidates[i] for i in indices if 0 <= i < len(candidates)]
            return selected
        except (ValueError, IndexError):
            return []

    def extract_candidates_with_diag(self, messages: List[Dict[str, str]]):
        """
        提取候选记忆，并返回诊断信息（用于验证小模型是否真的执行了）
        
        Returns:
            (candidates, diag): 候选列表 + 诊断字符串
        """
        if not messages:
            return [], "无对话历史"
        
        conversation = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
        
        try:
            result = self.chain.invoke({"conversation": conversation})
            # 兼容不同模型：content 可能为空，reasoning_content 可能有内容
            raw = (getattr(result, "content", "") or "").strip()
            if not raw:
                rc = getattr(result, "reasoning_content", "") or getattr(result, "additional_kwargs", {}).get("reasoning_content", "")
                raw = (rc or "").strip()
            if not raw:
                # 有些模型把文本放在 text 属性
                raw = (getattr(result, "text", "") or "").strip()
            candidates = self._parse_json(raw)
            
            if not isinstance(candidates, list):
                return [], f"小模型已执行，但返回非列表。原始输出:\n{raw[:300]}"
            
            valid = []
            for c in candidates:
                if isinstance(c, dict) and "content" in c:
                    valid.append({
                        "content": c["content"],
                        "category": c.get("category", "general"),
                        "confidence": c.get("confidence", "medium")
                    })
            
            if raw:
                diag = f"小模型已执行。输入 {len(messages)} 条消息。原始输出:\n{raw[:500]}"
            else:
                diag = f"小模型已执行但返回空。输入 {len(messages)} 条消息。模型对象: {repr(result)[:500]}"
            return valid, diag
            
        except Exception as e:
            return [], f"小模型执行失败: {e}"
