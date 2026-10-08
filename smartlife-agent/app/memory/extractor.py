"""
记忆提取器 - 从对话中提取候选记忆

原则：
- 只提取事实，不推断偏好
- 用户手动确认后才写入
- 允许用户编辑/删除
"""
import json
import re
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from app.config import create_small_llm
from app.observability import log_exception


class MemoryCandidate(BaseModel):
    content: str
    category: str = "general"
    confidence: str = "medium"
    type: str = "preference"
    data: Dict[str, Any] = Field(default_factory=dict)


class MemoryCandidateBatch(BaseModel):
    candidates: List[MemoryCandidate] = Field(default_factory=list)


@dataclass
class MemoryExtractionResult:
    """记忆提取的结构化结果。

    ``status`` 取值：
    - ``ok``：提取到可供用户确认的候选记忆；
    - ``no_content``：执行成功，但没有值得展示或保存的内容；
    - ``error``：提取器、模型调用、空响应或响应格式失败。
    """

    candidates: List[Dict[str, Any]] = field(default_factory=list)
    diagnostic: str = ""
    status: str = "no_content"

    @property
    def has_error(self) -> bool:
        return self.status == "error"

    @property
    def should_display(self) -> bool:
        """只有真实候选或失败诊断才值得在 UI 中出现。"""
        return bool(self.candidates) or self.has_error

    def as_dict(self) -> Dict[str, Any]:
        """转换为可进入 LangGraph State/SSE 的纯数据结构。"""
        return {
            "candidates": list(self.candidates),
            "diagnostic": self.diagnostic,
            "status": self.status,
            "has_error": self.has_error,
            "should_display": self.should_display,
        }


class MemoryExtractor:
    """记忆提取器"""

    _VALID_CATEGORIES = {"shopping", "travel", "general"}
    _VALID_CONFIDENCE = {"high", "medium", "low"}
    _LOW_VALUE_PREFIXES = ("我觉得", "我认为", "可能", "也许", "好像", "听说", "据说")
    _DESTINATIONS = (
        "上海", "杭州", "北京", "成都", "西安", "云南", "大理", "丽江",
        "三亚", "厦门", "桂林", "张家界", "西藏", "新疆",
    )

    def __init__(self):
        # reasoning token 也计入 completion 上限，过小会导致可见 JSON 为空。
        self.llm = create_small_llm(max_tokens=2048)
        
        # 简化提示词，避免 JSON 格式问题
        self.extract_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个记忆提取专家。从对话中提取有价值的事实信息。

提取原则：
1. 只提取明确提到的事实，不推断隐含信息
2. 不判断用户的偏好
3. 提取的信息应该是客观的、可验证的
4. 只根据本轮用户消息提取，不要根据助手回答或旧记忆补写事实

请提取以下类别的信息：
- 购物相关：商品名称、价格、数量、规格
- 旅行相关：目的地、日期、人数、预算
- 其他重要信息

请返回一个 JSON 对象，字段 candidates 是数组，每个元素包含：
- content: 事实描述
- category: shopping/travel/general
- confidence: high/medium/low

重要：只返回结构化对象。如果没有值得提取的信息，返回空 candidates。"""),
            ("user", "本轮用户信息：\n{conversation}")
        ])
        
        self.chain = self.extract_prompt | self.llm.with_structured_output(MemoryCandidateBatch)

    @staticmethod
    def _normalize_content(content: Any) -> str:
        return re.sub(r"\s+", " ", str(content or "")).strip()

    def _clean_candidates(
        self,
        candidates: Any,
        existing_contents: Optional[List[str]] = None,
        excluded_contents: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(candidates, list):
            return []
        accepted: List[Dict[str, Any]] = []
        seen = [
            re.sub(r"\s+", "", self._normalize_content(content)).casefold()
            for content in list(existing_contents or []) + list(excluded_contents or [])
            if self._normalize_content(content)
        ]
        for candidate in candidates:
            if not isinstance(candidate, dict) or "content" not in candidate:
                continue
            content = self._normalize_content(candidate.get("content"))
            if len(content) < 3 or len(content) > 500:
                continue
            if content.casefold().startswith(self._LOW_VALUE_PREFIXES):
                continue
            category = str(candidate.get("category", "general")).lower()
            confidence = str(candidate.get("confidence", "medium")).lower()
            normalized_key = re.sub(r"\s+", "", content).casefold()
            if any(
                SequenceMatcher(None, normalized_key, previous).ratio() >= 0.92
                for previous in seen
            ):
                continue
            accepted.append({
                "content": content,
                "category": category if category in self._VALID_CATEGORIES else "general",
                "confidence": confidence if confidence in self._VALID_CONFIDENCE else "medium",
            })
            seen.append(normalized_key)
            if len(accepted) >= 10:
                break
        return accepted

    @classmethod
    def _explicit_fact_fallback(cls, text: str) -> List[Dict[str, Any]]:
        """推理模型没有可见输出时，只兜底提取明确的旅行事实。"""
        facts = []
        destination = next((item for item in cls._DESTINATIONS if item in text), None)
        if destination:
            facts.append({
                "content": f"目的地为{destination}",
                "category": "travel",
                "confidence": "high",
            })
        days_match = re.search(r"(\d+)\s*(?:天|日)", text)
        if days_match:
            facts.append({
                "content": f"旅行天数为{days_match.group(1)}天",
                "category": "travel",
                "confidence": "high",
            })
        budget_match = re.search(r"(?:预算|花费|总预算)\s*(?:为|是|:|：)?\s*(\d+)\s*(?:元|块)", text)
        if budget_match:
            facts.append({
                "content": f"预算为{budget_match.group(1)}元",
                "category": "travel",
                "confidence": "high",
            })
        return facts

    def extract_candidates(
        self,
        messages: List[Dict[str, str]],
        existing_contents: Optional[List[str]] = None,
        excluded_contents: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """从对话中提取候选记忆。兼容旧接口，只返回候选列表。"""
        return self.extract_candidates_result(
            messages,
            existing_contents=existing_contents,
            excluded_contents=excluded_contents,
        ).candidates

    @staticmethod
    def _result_candidates(result: Any) -> Any:
        if isinstance(result, MemoryCandidateBatch):
            return [item.model_dump() for item in result.candidates]
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("candidates", result)
        if hasattr(result, "model_dump"):
            payload = result.model_dump()
            return payload.get("candidates", payload)
        raw = (getattr(result, "content", "") or "").strip()
        if not raw:
            raw = (
                getattr(result, "reasoning_content", "")
                or getattr(result, "additional_kwargs", {}).get("reasoning_content", "")
                or getattr(result, "text", "")
                or ""
            ).strip()
        return raw

    def extract_candidates_result(
        self,
        messages: List[Dict[str, str]],
        existing_contents: Optional[List[str]] = None,
        excluded_contents: Optional[List[str]] = None,
    ) -> MemoryExtractionResult:
        """提取候选记忆，并明确区分“没有内容”和“执行失败”。"""
        if not messages:
            return MemoryExtractionResult(diagnostic="无对话历史", status="no_content")

        user_messages = [
            self._normalize_content(msg.get("content", ""))
            for msg in messages
            if msg.get("role") == "user" and self._normalize_content(msg.get("content", ""))
        ][-1:]
        if not user_messages:
            return MemoryExtractionResult(status="no_content")
        conversation = "\n".join(user_messages)

        try:
            result = self.chain.invoke({
                "conversation": conversation,
            })
            raw = self._result_candidates(result)
            if not raw:
                fallback = self._clean_candidates(
                    self._explicit_fact_fallback(conversation),
                    existing_contents,
                    excluded_contents,
                )
                if fallback:
                    return MemoryExtractionResult(
                        candidates=fallback,
                        diagnostic="小模型无可见输出，已使用明确事实兜底提取。",
                        status="ok",
                    )
                return MemoryExtractionResult(
                    diagnostic="小模型无可见输出，本轮没有可提取的明确事实。",
                    status="no_content",
                )

            parsed = raw if isinstance(raw, list) else self._parse_json(str(raw))
            if not isinstance(parsed, list):
                return MemoryExtractionResult(
                    diagnostic=f"记忆提取失败：小模型返回非列表。原始输出:\n{raw[:300]}",
                    status="error",
                )
            if not parsed and isinstance(raw, str) and not re.search(r"\[\s*\]", raw):
                return MemoryExtractionResult(
                    diagnostic=f"记忆提取失败：无法解析小模型返回的 JSON。原始输出:\n{raw[:300]}",
                    status="error",
                )

            candidates = self._clean_candidates(parsed, existing_contents, excluded_contents)
            diagnostic = (
                f"小模型已执行。输入 {len(messages)} 条消息。"
                f"候选 {len(parsed)} 条，去重过滤后 {len(candidates)} 条。"
                f"原始输出:\n{raw[:500]}"
            )
            return MemoryExtractionResult(
                candidates=candidates,
                diagnostic=diagnostic,
                status="ok" if candidates else "no_content",
            )
        except Exception as e:
            log_exception("memory.extractor_result", e)
            return MemoryExtractionResult(
                diagnostic=f"记忆提取失败：{e}",
                status="error",
            )

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

    def extract_candidates_with_diag(
        self,
        messages: List[Dict[str, str]],
        existing_contents: Optional[List[str]] = None,
        excluded_contents: Optional[List[str]] = None,
    ):
        """兼容旧接口，返回 ``(candidates, diagnostic)``。"""
        result = self.extract_candidates_result(
            messages,
            existing_contents=existing_contents,
            excluded_contents=excluded_contents,
        )
        return result.candidates, result.diagnostic
