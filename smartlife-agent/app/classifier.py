"""
统一分类器 - 合并 Router + Intent 为一次小模型调用
减少一次 LLM 调用，延迟降低 1-2 秒

输出结构：
{
    "route": "react" | "plan_and_execute",
    "intent": "shopping" | "travel" | "negotiation" | "general",
    "sub_intent": "search" | "recommend" | "order_status" | ...,
    "retrieval": "sql_only" | "rag_only" | "mixed" | "none",
    "confidence": 0.0-1.0,
    "reason": "路由理由"
}
"""
import json
from app.streaming import EventType
import re
from typing import Dict, Any, List, Literal, Optional
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_small_llm
from app.observability import log_exception


class ClassificationResult(BaseModel):
    """分类结果"""
    route: Literal["react", "plan_and_execute"] = Field(
        default="react",
        description="路由策略：react用于简单任务，plan_and_execute用于复杂多步任务"
    )
    intent: Literal["shopping", "travel", "negotiation", "customer_service", "general"] = Field(
        default="general",
        description="意图类别"
    )
    sub_intent: str = Field(
        default="chat",
        description="子意图：search, recommend, order_status, refund, plan_trip, ..."
    )
    retrieval: Literal["sql_only", "rag_only", "mixed", "none"] = Field(
        default="none",
        description="检索策略：sql_only(结构化查询), rag_only(语义检索), mixed(混合), none(不需要检索)"
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="置信度"
    )
    reason: str = Field(
        default="",
        description="分类理由"
    )


# 快速规则：关键词匹配（零延迟，处理明确场景）
_ROUTE_KEYWORDS = {
    "plan_and_execute": [
        "规划", "计划", "清单", "安排", "预算分配", "帮我列", "行程", "装修",
        "准备", "策划", "方案", "统筹", "编排", "设计路线"
    ]
}

_INTENT_KEYWORDS = {
    "shopping": [
        "买", "找", "搜", "推荐", "商品",
        # 产品名词 - 用户提到这些词就是在找商品
        "鞋", "衣服", "手机", "电脑", "平板", "耳机", "背包", "帐篷",
        "冲锋衣", "睡袋", "泳衣", "T恤", "衬衫", "裤子", "外套",
        "连衣裙", "裙子", "跑步鞋", "登山鞋", "运动鞋",
        "哪个好", "哪个更好", "对比", "比较", "区别",
        # 品牌名
        "小米", "iPhone", "苹果", "华为", "Nike", "Adidas", "阿迪",
        "优衣库", "ZARA", "波司登", "Salomon", "迪卡侬", "安踏", "李宁",
    ],
    "travel": [
        "旅行", "旅游", "酒店", "景点", "机票", "出行", "度假", "目的地", "行程", "攻略",
        "好玩", "风景", "景区", "民宿", "住宿", "路线", "几天", "天游",
        "杭州", "北京", "上海", "成都", "西安", "云南", "大理", "丽江",
        "三亚", "厦门", "桂林", "张家界", "西藏", "新疆",
        "规划", "两天", "三天", "几天", "一日游", "自驾游", "周边游",
    ],
    "negotiation": ["一起", "朋友", "大家", "协商", "都同意", "投票"],
    "customer_service": ["退", "换", "订单", "物流", "投诉", "退款", "售后", "查订单", "订单状态"],
}

_SHOPPING_WEAK_KEYWORDS = [
    "价格", "便宜", "贵", "打折", "优惠", "多少钱", "元的", "块钱", "预算",
]
_GENERIC_SHOPPING_SIGNALS = {
    "找", "搜", "推荐", "哪个好", "哪个更好", "对比", "比较", "区别",
}
_TRAVEL_DURATION_RE = re.compile(
    r"(?:玩|游|待|停留)\s*\d+\s*(?:天|日)|\d+\s*(?:天|日)\s*(?:游|行程|攻略)"
)


def _has_strong_shopping_signal(message: str) -> bool:
    return any(
        keyword in message
        for keyword in _INTENT_KEYWORDS["shopping"]
        if keyword not in _GENERIC_SHOPPING_SIGNALS
    )


def _has_travel_signal(message: str) -> bool:
    return (
        any(keyword in message for keyword in _INTENT_KEYWORDS["travel"])
        or bool(_TRAVEL_DURATION_RE.search(message))
    )


def _keyword_classify(message: str) -> Optional[Dict[str, Any]]:
    """关键词快速分类（零延迟）"""
    route = "react"
    if (
        any(kw in message for kw in _ROUTE_KEYWORDS.get("plan_and_execute", []))
        or _TRAVEL_DURATION_RE.search(message)
    ):
        route = "plan_and_execute"

    intent = "general"
    max_match = 0
    match_counts = {
        intent_type: sum(1 for kw in keywords if kw in message)
        for intent_type, keywords in _INTENT_KEYWORDS.items()
    }
    travel_match_count = match_counts["travel"] + bool(_TRAVEL_DURATION_RE.search(message))

    # 金额/预算词既可能属于购物，也可能属于旅行。没有商品动作或商品名词时，
    # 不能让这些弱购物信号覆盖目的地、游玩天数等明确旅游信号。
    if travel_match_count and not _has_strong_shopping_signal(message):
        intent = "travel"
        max_match = travel_match_count
    else:
        for intent_type, keywords in _INTENT_KEYWORDS.items():
            match_count = match_counts[intent_type]
            if intent_type == "travel":
                match_count = travel_match_count
            elif intent_type == "shopping":
                match_count += sum(1 for kw in _SHOPPING_WEAK_KEYWORDS if kw in message)
            if match_count > max_match:
                max_match = match_count
                intent = intent_type

    if max_match >= 2:
        # 购物意图必须有检索策略
        retrieval = "none"
        if intent == "shopping":
            retrieval = "mixed"
        elif intent == "travel":
            retrieval = "rag_only"
        return {
            "route": route,
            "intent": intent,
            "sub_intent": (
                "plan_trip"
                if intent == "travel" and route == "plan_and_execute"
                else "qa"
                if intent == "travel"
                else "search"
                if intent in {"shopping", "customer_service"}
                else "chat"
            ),
            "retrieval": retrieval,
            "confidence": 0.85,
            "reason": f"关键词匹配 ({max_match} 个关键词命中)",
            "from": "keyword"
        }
    return None


# 小模型分类 prompt
_CLASSIFY_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是 SmartLife Agent 的分诊系统。分析用户消息，一次性返回分类结果。

路由规则（route）：
- "react": 单次查询、导购对话、客服问答、简单问答、单步操作
- "plan_and_execute": 多步规划（行程、采购清单、预算分配）、跨场景任务、需要用户确认计划、复杂决策

意图规则（intent）：
- "shopping": 购物相关（商品搜索、导购、评价）
- "customer_service": 客服相关（订单查询、退换货、投诉）
- "travel": 旅游相关（行程规划、目的地推荐、酒店查询）
- "negotiation": 社交协商（多人出行、偏好协调）
- "general": 通用对话

检索策略（retrieval）：
- "sql_only": 查商品属性（价格、分类、品牌、库存、排序）
- "rag_only": 查评价口碑、攻略内容、FAQ
- "mixed": 同时需要商品数据和评价（大多数购物查询用这个）
- "none": 仅用于纯闲聊、政策问答等不需要查数据的场景

重要：只要涉及找商品、推荐商品、查价格，必须用 "mixed" 或 "sql_only"，绝不能用 "none"！

严格返回JSON，不要其他文字：
{{"route": "...", "intent": "...", "sub_intent": "...", "retrieval": "...", "confidence": 0.X, "reason": "..."}}"""),
            ("user", "对话上下文：{context}\n\n当前消息：{message}")
])


def _parse_json_response(content: str) -> Dict[str, Any]:
    """解析 LLM 的 JSON 响应，兼容多种格式"""
    # 尝试提取 ```json ... ```
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
    if json_match:
        return json.loads(json_match.group(1))

    # 尝试直接解析
    json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
    if json_match:
        return json.loads(json_match.group(0))

    return {}


class UnifiedClassifier:
    """
    统一分类器
    第一层：关键词快速判断（零延迟）
    第二层：小模型判断（处理模糊情况）
    """

    def __init__(self):
        self._llm = None
        self._chain = None

    @property
    def llm(self):
        if self._llm is None:
            self._llm = create_small_llm(max_tokens=300)
        return self._llm

    @property
    def chain(self):
        if self._chain is None:
            self._chain = _CLASSIFY_PROMPT | self.llm
        return self._chain

    @staticmethod
    def _format_context(history: Optional[List[Dict[str, Any]]]) -> str:
        if not history:
            return "无"
        lines = []
        for item in list(history)[-6:]:
            role = item.get("role", "message") if isinstance(item, dict) else getattr(item, "type", "message")
            content = item.get("content", "") if isinstance(item, dict) else getattr(item, "content", str(item))
            lines.append(f"{role}: {content}")
        return "\n".join(lines) or "无"

    @staticmethod
    def _uses_reference(message: str) -> bool:
        reference_terms = (
            "这个", "那个", "它", "他们", "第二个", "刚才", "之前", "上面",
            "继续", "还是", "改成", "按刚才", "就按", "同上", "那个方案",
        )
        return any(term in message for term in reference_terms)

    def _context_fallback(
        self,
        message: str,
        previous: Optional[ClassificationResult],
    ) -> Optional[ClassificationResult]:
        if not previous or not self._uses_reference(message):
            return None
        return previous.model_copy(update={
            "confidence": max(0.55, min(previous.confidence, 0.8)),
            "reason": f"多轮指代，沿用上一轮分类：{previous.intent}/{previous.route}",
        })

    @staticmethod
    def _from_data(data: Dict[str, Any], reason_default: str = "小模型分类") -> ClassificationResult:
        return ClassificationResult(
            route=data.get("route", "react"),
            intent=data.get("intent", "general"),
            sub_intent=data.get("sub_intent", "chat"),
            retrieval=data.get("retrieval", "none"),
            confidence=data.get("confidence", 0.5),
            reason=data.get("reason", reason_default),
        )

    @staticmethod
    def apply_scene_constraint(
        classification: ClassificationResult,
        scene: str,
        message: str,
    ) -> ClassificationResult:
        """让 UI 场景成为路由约束，防止通用金额词跨场景误判。"""
        if scene != "travel":
            return classification

        explicit_shopping = _has_strong_shopping_signal(message)
        travel_request = _has_travel_signal(message)
        if explicit_shopping and classification.intent in {"shopping", "customer_service"}:
            return classification
        if not travel_request:
            return classification

        if classification.intent == "travel":
            return classification

        needs_plan = (
            classification.route == "plan_and_execute"
            or bool(_TRAVEL_DURATION_RE.search(message))
        )
        return classification.model_copy(update={
            "route": "plan_and_execute" if needs_plan else classification.route,
            "intent": "travel",
            "sub_intent": "plan_trip" if needs_plan else "qa",
            "retrieval": "rag_only",
            "reason": (
                f"旅游场景约束：从 {classification.intent}/{classification.retrieval} "
                "纠正为 travel/rag_only"
            ),
        })

    def classify(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        previous: Optional[ClassificationResult] = None,
    ) -> ClassificationResult:
        """
        同步分类（兼容旧代码）

        Args:
            message: 用户输入

        Returns:
            ClassificationResult: 分类结果
        """
        # 第一层：关键词快速判断
        keyword_result = _keyword_classify(f"{self._format_context(history)}\n{message}")
        if keyword_result:
            return ClassificationResult(**{
                k: v for k, v in keyword_result.items()
                if k in ClassificationResult.model_fields
            })

        context_fallback = self._context_fallback(message, previous)
        if context_fallback:
            return context_fallback

        # 第二层：小模型判断
        try:
            result = self.chain.invoke({
                "message": message,
                "context": self._format_context(history),
            })
            data = _parse_json_response(result.content)
            return self._from_data(data)
        except Exception as e:
            log_exception("classifier.sync", e, {"message": message})
            # 默认降级
            return ClassificationResult(
                route="react",
                intent="general",
                sub_intent="chat",
                retrieval="none",
                confidence=0.3,
                reason=f"分类失败，使用默认值: {str(e)[:50]}"
            )

    async def aclassify(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        previous: Optional[ClassificationResult] = None,
    ) -> ClassificationResult:
        """
        异步分类（用于 streaming 流程）

        Args:
            message: 用户输入

        Returns:
            ClassificationResult: 分类结果
        """
        # 第一层：关键词快速判断
        keyword_result = _keyword_classify(f"{self._format_context(history)}\n{message}")
        if keyword_result:
            return ClassificationResult(**{
                k: v for k, v in keyword_result.items()
                if k in ClassificationResult.model_fields
            })

        context_fallback = self._context_fallback(message, previous)
        if context_fallback:
            return context_fallback

        # 第二层：小模型异步调用
        try:
            result = await self.chain.ainvoke({
                "message": message,
                "context": self._format_context(history),
            })
            data = _parse_json_response(result.content)
            return self._from_data(data)
        except Exception as e:
            log_exception("classifier.async", e, {"message": message})
            return ClassificationResult(
                route="react",
                intent="general",
                sub_intent="chat",
                retrieval="none",
                confidence=0.3,
                reason=f"分类失败，使用默认值: {str(e)[:50]}"
            )

    async def aclassify_streaming(
        self,
        message: str,
        queue: 'EventQueue',
        history: Optional[List[Dict[str, Any]]] = None,
        previous: Optional[ClassificationResult] = None,
    ) -> ClassificationResult:
        """
        流式分类 - 推送思考过程事件

        Args:
            message: 用户输入
            queue: 事件队列

        Returns:
            ClassificationResult: 分类结果
        """
        # 第一层：关键词快速判断
        keyword_result = _keyword_classify(f"{self._format_context(history)}\n{message}")
        if keyword_result:
            await queue.emit(EventType.CLASSIFY, {
                **keyword_result,
                "method": "keyword"
            }, step="classify")
            return ClassificationResult(**{
                k: v for k, v in keyword_result.items()
                if k in ClassificationResult.model_fields
            })

        context_fallback = self._context_fallback(message, previous)
        if context_fallback:
            await queue.emit(EventType.CLASSIFY, {
                "route": context_fallback.route,
                "intent": context_fallback.intent,
                "sub_intent": context_fallback.sub_intent,
                "retrieval": context_fallback.retrieval,
                "confidence": context_fallback.confidence,
                "reason": context_fallback.reason,
                "method": "context",
            }, step="classify")
            return context_fallback

        # 第二层：小模型流式判断
        await queue.emit_thinking("🧠 小模型正在分析您的需求...", step="classify")

        try:
            full_content = ""
            async for chunk in self.chain.astream({
                "message": message,
                "context": self._format_context(history),
            }):
                full_content += chunk.content

            data = _parse_json_response(full_content)
            result = self._from_data(data)

            await queue.emit(EventType.CLASSIFY, {
                "route": result.route,
                "intent": result.intent,
                "sub_intent": result.sub_intent,
                "retrieval": result.retrieval,
                "confidence": result.confidence,
                "reason": result.reason,
                "method": "llm"
            }, step="classify")

            return result
        except Exception as e:
            log_exception("classifier.streaming", e, {"message": message})
            fallback = ClassificationResult(
                route="react",
                intent="general",
                sub_intent="chat",
                retrieval="none",
                confidence=0.3,
                reason=f"分类失败: {str(e)[:50]}"
            )
            await queue.emit(EventType.ERROR, {
                "message": f"分类失败，使用默认值: {str(e)[:80]}"
            }, step="classify")
            return fallback


# 便捷函数
_classifier = None

def get_classifier() -> UnifiedClassifier:
    global _classifier
    if _classifier is None:
        _classifier = UnifiedClassifier()
    return _classifier

def classify_message(message: str) -> ClassificationResult:
    """快速分类"""
    return get_classifier().classify(message)
