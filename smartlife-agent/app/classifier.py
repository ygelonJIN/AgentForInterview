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
from typing import Dict, Any, Literal, Optional
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_small_llm


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
        "买", "找", "搜", "推荐", "商品", "价格", "便宜", "贵", "打折", "优惠",
        # 产品名词 - 用户提到这些词就是在找商品
        "鞋", "衣服", "手机", "电脑", "平板", "耳机", "背包", "帐篷",
        "冲锋衣", "睡袋", "泳衣", "T恤", "衬衫", "裤子", "外套",
        "连衣裙", "裙子", "跑步鞋", "登山鞋", "运动鞋",
        "多少钱", "元的", "块钱", "预算",
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


def _keyword_classify(message: str) -> Optional[Dict[str, Any]]:
    """关键词快速分类（零延迟）"""
    route = "react"
    for kw in _ROUTE_KEYWORDS.get("plan_and_execute", []):
        if kw in message:
            route = "plan_and_execute"
            break

    intent = "general"
    max_match = 0
    for intent_type, keywords in _INTENT_KEYWORDS.items():
        match_count = sum(1 for kw in keywords if kw in message)
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
- "shopping": 购物相关（商品搜索、导购、下单）
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
    ("user", "{message}")
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

    def classify(self, message: str) -> ClassificationResult:
        """
        同步分类（兼容旧代码）

        Args:
            message: 用户输入

        Returns:
            ClassificationResult: 分类结果
        """
        # 第一层：关键词快速判断
        keyword_result = _keyword_classify(message)
        if keyword_result:
            return ClassificationResult(**{
                k: v for k, v in keyword_result.items()
                if k in ClassificationResult.model_fields
            })

        # 第二层：小模型判断
        try:
            result = self.chain.invoke({"message": message})
            data = _parse_json_response(result.content)
            return ClassificationResult(
                route=data.get("route", "react"),
                intent=data.get("intent", "general"),
                sub_intent=data.get("sub_intent", "chat"),
                retrieval=data.get("retrieval", "none"),
                confidence=data.get("confidence", 0.5),
                reason=data.get("reason", "小模型分类")
            )
        except Exception as e:
            # 默认降级
            return ClassificationResult(
                route="react",
                intent="general",
                sub_intent="chat",
                retrieval="none",
                confidence=0.3,
                reason=f"分类失败，使用默认值: {str(e)[:50]}"
            )

    async def aclassify(self, message: str) -> ClassificationResult:
        """
        异步分类（用于 streaming 流程）

        Args:
            message: 用户输入

        Returns:
            ClassificationResult: 分类结果
        """
        # 第一层：关键词快速判断
        keyword_result = _keyword_classify(message)
        if keyword_result:
            return ClassificationResult(**{
                k: v for k, v in keyword_result.items()
                if k in ClassificationResult.model_fields
            })

        # 第二层：小模型异步调用
        try:
            result = await self.chain.ainvoke({"message": message})
            data = _parse_json_response(result.content)
            return ClassificationResult(
                route=data.get("route", "react"),
                intent=data.get("intent", "general"),
                sub_intent=data.get("sub_intent", "chat"),
                retrieval=data.get("retrieval", "none"),
                confidence=data.get("confidence", 0.5),
                reason=data.get("reason", "小模型分类")
            )
        except Exception as e:
            return ClassificationResult(
                route="react",
                intent="general",
                sub_intent="chat",
                retrieval="none",
                confidence=0.3,
                reason=f"分类失败，使用默认值: {str(e)[:50]}"
            )

    async def aclassify_streaming(self, message: str, queue: 'EventQueue') -> ClassificationResult:
        """
        流式分类 - 推送思考过程事件

        Args:
            message: 用户输入
            queue: 事件队列

        Returns:
            ClassificationResult: 分类结果
        """
        # 第一层：关键词快速判断
        keyword_result = _keyword_classify(message)
        if keyword_result:
            await queue.emit(EventType.CLASSIFY, {
                "result": keyword_result,
                "method": "keyword"
            }, step="classify")
            return ClassificationResult(**{
                k: v for k, v in keyword_result.items()
                if k in ClassificationResult.model_fields
            })

        # 第二层：小模型流式判断
        await queue.emit_thinking("🧠 小模型正在分析您的需求...", step="classify")

        try:
            full_content = ""
            async for chunk in self.chain.astream({"message": message}):
                full_content += chunk.content

            data = _parse_json_response(full_content)
            result = ClassificationResult(
                route=data.get("route", "react"),
                intent=data.get("intent", "general"),
                sub_intent=data.get("sub_intent", "chat"),
                retrieval=data.get("retrieval", "none"),
                confidence=data.get("confidence", 0.5),
                reason=data.get("reason", "小模型分类")
            )

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
