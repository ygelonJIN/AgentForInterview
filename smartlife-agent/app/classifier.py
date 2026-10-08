"""
统一分类器 - 合并 Router + Intent 为一次小模型调用
减少一次 LLM 调用，延迟降低 1-2 秒

输出结构：
{
    "route": "react" | "plan_and_execute",
    "intent": "shopping" | "travel" | "negotiation" | "customer_service" | "general",
    "intents": ["shopping", "travel"],  # 明确复合任务可保留 1-2 个意图
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
from pydantic import BaseModel, Field, model_validator
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_llm, create_small_llm
from app.observability import extract_model_usage, log_exception, timed_span


LOW_CONFIDENCE_THRESHOLD = 0.55


class ClassificationResponseError(ValueError):
    """分类模型输出为空、JSON 非法或缺少必要字段。"""


class ClassificationResult(BaseModel):
    """分类结果"""
    route: Literal["react", "plan_and_execute"] = Field(
        default="react",
        description="路由策略：react用于简单任务，plan_and_execute用于复杂多步任务"
    )
    intent: Literal["shopping", "travel", "negotiation", "customer_service", "general"] = Field(
        default="general",
        description="主意图类别；兼容旧的单意图字段"
    )
    intents: List[Literal["shopping", "travel", "negotiation", "customer_service", "general"]] = Field(
        default_factory=list,
        description="明确的多意图集合；为空时等价于 [intent]"
    )
    primary_intent: Optional[Literal["shopping", "travel", "negotiation", "customer_service", "general"]] = Field(
        default=None,
        description="主意图；与 intent 保持一致，便于多意图结果显式表达"
    )
    intent_scores: Dict[str, float] = Field(
        default_factory=dict,
        description="关键词或模型给出的各意图证据分数/置信度"
    )
    sub_intent: str = Field(
        default="chat",
        min_length=1,
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

    @model_validator(mode="after")
    def _normalize_intents(self):
        """把单意图兼容字段和多意图列表归一化，主 Intent 始终排在第一位。"""
        if self.primary_intent:
            self.intent = self.primary_intent
        self.primary_intent = self.intent
        if not self.intents:
            self.intents = [self.intent]
            return self
        deduped = list(dict.fromkeys(self.intents))
        if self.intent in deduped:
            deduped.remove(self.intent)
            deduped.insert(0, self.intent)
        else:
            self.intent = deduped[0]
        self.intents = deduped[:2]
        return self

    @property
    def effective_intents(self) -> List[str]:
        """兼容旧代码：未显式给出 intents 时退化为单意图。"""
        return list(self.intents or [self.intent])

    @property
    def agent_label(self) -> str:
        return "+".join(self.effective_intents)


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
        "规划", "两天", "三天", "一日游", "自驾游", "周边游",
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
    strong_match_counts = {
        intent_type: sum(1 for kw in keywords if kw in message)
        for intent_type, keywords in _INTENT_KEYWORDS.items()
    }
    travel_match_count = (
        strong_match_counts["travel"] + bool(_TRAVEL_DURATION_RE.search(message))
    )
    shopping_weak_match_count = sum(
        1 for kw in _SHOPPING_WEAK_KEYWORDS if kw in message
    )
    match_counts = dict(strong_match_counts)
    match_counts["shopping"] += shopping_weak_match_count

    # 明确的复合意图：多个类别都达到独立证据阈值时，保留多个意图。
    # 购物必须有强购物词，不能只靠价格/预算弱词；Travel 必须至少有两个信号。
    candidate_scores = {}
    shopping_strong_count = sum(
        1 for kw in _INTENT_KEYWORDS["shopping"]
        if kw in message and kw not in _GENERIC_SHOPPING_SIGNALS
    )
    shopping_total_count = strong_match_counts["shopping"] + shopping_weak_match_count
    if shopping_total_count >= 2 and shopping_strong_count > 0:
        candidate_scores["shopping"] = shopping_total_count
    if travel_match_count >= 2:
        candidate_scores["travel"] = travel_match_count
    if strong_match_counts["negotiation"] >= 2:
        candidate_scores["negotiation"] = strong_match_counts["negotiation"]
    if strong_match_counts["customer_service"] >= 2:
        candidate_scores["customer_service"] = strong_match_counts["customer_service"]
    if len(candidate_scores) >= 2:
        priority = {"shopping": 0, "travel": 1, "negotiation": 2, "customer_service": 3}
        ordered = sorted(
            candidate_scores,
            key=lambda item: (-candidate_scores[item], priority[item]),
        )[:2]
        retrieval = "mixed" if "shopping" in ordered else "rag_only"
        sub_intents = []
        for item in ordered:
            if item == "travel":
                sub_intents.append("plan_trip" if route == "plan_and_execute" else "qa")
            elif item in {"shopping", "customer_service"}:
                sub_intents.append("search")
            else:
                sub_intents.append("chat")
        return {
            "route": route,
            "intent": ordered[0],
            "intents": ordered,
            "intent_scores": {key: float(value) for key, value in candidate_scores.items()},
            "sub_intent": "+".join(sub_intents),
            "retrieval": retrieval,
            "confidence": 0.9,
            "reason": (
                "多意图关键词匹配: "
                + ", ".join(f"{key}={candidate_scores[key]}" for key in ordered)
            ),
            "from": "keyword",
        }

    # 金额/预算词既可能属于购物，也可能属于旅行。没有商品动作或商品名词时，
    # 不能让这些弱购物信号覆盖目的地、游玩天数等明确旅游信号。
    if travel_match_count >= 2 and not _has_strong_shopping_signal(message):
        intent = "travel"
        max_match = travel_match_count
    else:
        for intent_type in _INTENT_KEYWORDS:
            match_count = match_counts[intent_type]
            if intent_type == "travel":
                match_count = travel_match_count
            if match_count > max_match:
                max_match = match_count
                intent = intent_type

    # 两个弱价格词不能单独把任意请求升级成购物；至少需要一个商品/购物强信号。
    shopping_has_required_strong_signal = (
        intent != "shopping" or strong_match_counts["shopping"] > 0
    )
    if max_match >= 2 and shopping_has_required_strong_signal:
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
            "reason": (
                f"关键词匹配 ({max_match} 个关键词命中"
                f"{', 含弱购物词 ' + str(shopping_weak_match_count) if shopping_weak_match_count else ''})"
            ),
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

多意图规则：
- 如果一句话明确包含两个独立任务（例如“推荐跑鞋，再制定杭州两天计划”），必须同时输出两个意图。
- intent 放主意图；intents 放完整意图列表，最多两个；primary_intent 放主意图；intent_scores 给每个意图 0-1 分。
- 两个意图都很明确时不要强行二选一。复合任务 route 通常使用 "plan_and_execute"。

检索策略（retrieval）：
- "sql_only": 查商品属性（价格、分类、品牌、库存、排序）
- "rag_only": 查评价口碑、攻略内容、FAQ
- "mixed": 同时需要商品数据和评价（大多数购物查询用这个）
- "none": 仅用于纯闲聊、政策问答等不需要查数据的场景

重要：只要涉及找商品、推荐商品、查价格，必须用 "mixed" 或 "sql_only"，绝不能用 "none"！

严格返回JSON，不要其他文字：
{{"route": "...", "intent": "...", "intents": ["..."], "primary_intent": "...", "intent_scores": {{"...": 0.X}}, "sub_intent": "...", "retrieval": "...", "confidence": 0.X, "reason": "..."}}"""),
            ("user", "对话上下文：{context}\n\n当前消息：{message}")
])

_CLASSIFY_ARBITER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是分类纠错与仲裁器。独立分析用户当前消息，并检查候选分类是否合理。

只允许输出一个严格 JSON 对象，必须包含：
route、intent、intents、primary_intent、intent_scores、sub_intent、retrieval、confidence、reason。

枚举限制：
- route: react / plan_and_execute
- intent / intents / primary_intent: shopping / travel / negotiation / customer_service / general
- intents 是 1-2 个明确意图；复合请求不能只保留一个
- retrieval: sql_only / rag_only / mixed / none

如果当前消息出现新的明确任务，不要沿用候选分类；如果是连续追问，可以保留原意图。
不要输出解释、Markdown 或 JSON 以外的内容。"""),
    ("user", """模式：{mode}
对话上下文：{context}
当前消息：{message}
候选分类：{candidate_json}
校验错误：{error}

请返回修正后的分类 JSON。""")
])


def _parse_json_response(content: str) -> Dict[str, Any]:
    """从模型输出中提取第一个完整 JSON 对象；失败时明确报错。"""
    if not isinstance(content, str) or not content.strip():
        raise ClassificationResponseError("分类模型没有可见输出")

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
    candidates = [fenced.group(1), content] if fenced else [content]
    decoder = json.JSONDecoder()

    for candidate in candidates:
        candidate = candidate.strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = None
            for index, char in enumerate(candidate):
                if char != "{":
                    continue
                try:
                    parsed, _ = decoder.raw_decode(candidate[index:])
                    break
                except json.JSONDecodeError:
                    continue
        if isinstance(parsed, dict):
            return parsed
        if parsed is not None:
            break

    raise ClassificationResponseError("分类模型输出不是 JSON 对象")


class UnifiedClassifier:
    """
    统一分类器
    第一层：关键词快速判断（零延迟）
    第二层：小模型判断（处理模糊情况）
    """

    def __init__(self):
        self._llm = None
        self._chain = None
        self._arbiter_llm = None
        self._arbiter_chain = None

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

    @property
    def arbiter_llm(self):
        if self._arbiter_llm is None:
            self._arbiter_llm = create_llm(max_tokens=300)
        return self._arbiter_llm

    @property
    def arbiter_chain(self):
        if self._arbiter_chain is None:
            self._arbiter_chain = _CLASSIFY_ARBITER_PROMPT | self.arbiter_llm
        return self._arbiter_chain

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

    @staticmethod
    def _has_task_switch_signal(message: str) -> bool:
        """明确的新任务信号不应被上一轮分类锁死。"""
        return (
            _has_strong_shopping_signal(message)
            or _has_travel_signal(message)
            or any(keyword in message for keyword in _INTENT_KEYWORDS["customer_service"])
            or any(keyword in message for keyword in _INTENT_KEYWORDS["negotiation"])
        )

    def _context_fallback(
        self,
        message: str,
        previous: Optional[ClassificationResult],
    ) -> Optional[ClassificationResult]:
        if (
            not previous
            or not self._uses_reference(message)
            or self._has_task_switch_signal(message)
        ):
            return None
        return previous.model_copy(update={
            "confidence": max(0.55, min(previous.confidence, 0.8)),
            "reason": f"多轮指代，沿用上一轮分类：{previous.intent}/{previous.route}",
        })

    @staticmethod
    def _from_data(
        data: Dict[str, Any],
        reason_default: str = "小模型分类",
        *,
        strict: bool = True,
    ) -> ClassificationResult:
        if not isinstance(data, dict):
            raise ClassificationResponseError("分类结果不是对象")
        if strict:
            required = {"route", "intent", "sub_intent", "retrieval", "confidence"}
            missing = sorted(required - data.keys())
            if missing:
                raise ClassificationResponseError(
                    "分类结果缺少字段: " + ", ".join(missing)
                )
            for field in ("route", "intent", "sub_intent", "retrieval"):
                if not isinstance(data[field], str) or not data[field].strip():
                    raise ClassificationResponseError(f"分类字段 {field} 必须是非空字符串")
            if (
                isinstance(data["confidence"], bool)
                or not isinstance(data["confidence"], (int, float))
            ):
                raise ClassificationResponseError("分类字段 confidence 必须是数字")
        raw_intents = data.get("intents")
        if raw_intents is None:
            raw_intents = [data.get("intent", "general")]
        if not isinstance(raw_intents, list) or not raw_intents:
            raise ClassificationResponseError("intents 必须是非空列表")
        if len(raw_intents) > 2:
            raise ClassificationResponseError("intents 最多包含两个明确意图")
        if any(not isinstance(item, str) or not item.strip() for item in raw_intents):
            raise ClassificationResponseError("intents 只能包含非空字符串")
        ordered_intents = list(dict.fromkeys(item.strip() for item in raw_intents))
        primary_intent = data.get("primary_intent") or data.get("intent", "general")
        if primary_intent not in ordered_intents:
            raise ClassificationResponseError("primary_intent 必须包含在 intents 中")
        ordered_intents.remove(primary_intent)
        ordered_intents.insert(0, primary_intent)
        raw_scores = data.get("intent_scores") or {}
        if not isinstance(raw_scores, dict):
            raise ClassificationResponseError("intent_scores 必须是对象")
        intent_scores = {}
        for key, value in raw_scores.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ClassificationResponseError("intent_scores 的值必须是数字")
            intent_scores[str(key)] = float(value)
        payload = {
            "route": data.get("route", "react"),
            "intent": primary_intent,
            "primary_intent": primary_intent,
            "intents": ordered_intents,
            "intent_scores": intent_scores,
            "sub_intent": data.get("sub_intent", "chat"),
            "retrieval": data.get("retrieval", "none"),
            "confidence": data.get("confidence", 0.5),
            "reason": data.get("reason", reason_default),
        }
        try:
            return ClassificationResult.model_validate(payload)
        except Exception as exc:
            raise ClassificationResponseError(f"分类字段校验失败: {exc}") from exc

    def _arbiter_values(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]],
        candidate: Optional[ClassificationResult],
        mode: str,
        error: str = "",
    ) -> Dict[str, Any]:
        candidate_data = candidate.model_dump() if candidate else {}
        return {
            "mode": mode,
            "context": self._format_context(history),
            "message": message,
            "candidate_json": json.dumps(candidate_data, ensure_ascii=False),
            "error": error or "无",
        }

    def _arbitrate_sync(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]],
        candidate: Optional[ClassificationResult],
        mode: str,
        error: str = "",
        trace_id: str = "",
    ) -> ClassificationResult:
        with timed_span(
            "model.classify_arbiter",
            trace_id=trace_id,
            attributes={"mode": mode},
        ) as measurement:
            result = self.arbiter_chain.invoke(
                self._arbiter_values(message, history, candidate, mode, error)
            )
            measurement.add_attributes(extract_model_usage(result))
        return self._from_data(
            _parse_json_response(result.content),
            reason_default="主模型分类仲裁",
        )

    async def _arbitrate_async(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]],
        candidate: Optional[ClassificationResult],
        mode: str,
        error: str = "",
        trace_id: str = "",
    ) -> ClassificationResult:
        with timed_span(
            "model.classify_arbiter",
            trace_id=trace_id,
            attributes={"mode": mode},
        ) as measurement:
            result = await self.arbiter_chain.ainvoke(
                self._arbiter_values(message, history, candidate, mode, error)
            )
            measurement.add_attributes(extract_model_usage(result))
        return self._from_data(
            _parse_json_response(result.content),
            reason_default="主模型分类仲裁",
        )

    @staticmethod
    def _fallback(error: str) -> ClassificationResult:
        return ClassificationResult(
            route="react",
            intent="general",
            intents=["general"],
            intent_scores={"general": 0.3},
            sub_intent="chat",
            retrieval="none",
            confidence=0.3,
            reason=f"分类失败，使用默认值: {error[:50]}",
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
        # 第一层：只对当前消息做关键词判断，历史仅提供给模型上下文。
        keyword_result = _keyword_classify(message)
        if keyword_result:
            return ClassificationResult(**{
                k: v for k, v in keyword_result.items()
                if k in ClassificationResult.model_fields
            })

        context_fallback = self._context_fallback(message, previous)
        if context_fallback:
            return context_fallback

        # 第二层：小模型判断；低置信或格式失败时只做一次主模型仲裁。
        candidate: Optional[ClassificationResult] = None
        try:
            result = self.chain.invoke({
                "message": message,
                "context": self._format_context(history),
            })
            data = _parse_json_response(result.content)
            candidate = self._from_data(data)
            if candidate.confidence >= LOW_CONFIDENCE_THRESHOLD:
                return candidate
            try:
                return self._arbitrate_sync(
                    message,
                    history,
                    candidate,
                    mode="low_confidence",
                )
            except Exception as arbiter_error:
                log_exception("classifier.arbiter.sync", arbiter_error, {"message": message})
                return candidate.model_copy(update={
                    "reason": candidate.reason + "；主模型仲裁失败，保留低置信分类",
                })
        except Exception as e:
            log_exception("classifier.sync", e, {"message": message})
            try:
                return self._arbitrate_sync(
                    message,
                    history,
                    candidate,
                    mode="repair",
                    error=str(e),
                )
            except Exception as arbiter_error:
                log_exception("classifier.arbiter.sync", arbiter_error, {"message": message})
                return self._fallback(f"{e}; 仲裁失败: {arbiter_error}")

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
        # 第一层：只对当前消息做关键词判断，历史仅提供给模型上下文。
        keyword_result = _keyword_classify(message)
        if keyword_result:
            return ClassificationResult(**{
                k: v for k, v in keyword_result.items()
                if k in ClassificationResult.model_fields
            })

        context_fallback = self._context_fallback(message, previous)
        if context_fallback:
            return context_fallback

        # 第二层：小模型异步调用；低置信或格式失败时只做一次主模型仲裁。
        candidate: Optional[ClassificationResult] = None
        try:
            result = await self.chain.ainvoke({
                "message": message,
                "context": self._format_context(history),
            })
            data = _parse_json_response(result.content)
            candidate = self._from_data(data)
            if candidate.confidence >= LOW_CONFIDENCE_THRESHOLD:
                return candidate
            try:
                return await self._arbitrate_async(
                    message,
                    history,
                    candidate,
                    mode="low_confidence",
                )
            except Exception as arbiter_error:
                log_exception("classifier.arbiter.async", arbiter_error, {"message": message})
                return candidate.model_copy(update={
                    "reason": candidate.reason + "；主模型仲裁失败，保留低置信分类",
                })
        except Exception as e:
            log_exception("classifier.async", e, {"message": message})
            try:
                return await self._arbitrate_async(
                    message,
                    history,
                    candidate,
                    mode="repair",
                    error=str(e),
                )
            except Exception as arbiter_error:
                log_exception("classifier.arbiter.async", arbiter_error, {"message": message})
                return self._fallback(f"{e}; 仲裁失败: {arbiter_error}")

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
        # 第一层：只对当前消息做关键词判断，历史仅提供给模型上下文。
        keyword_result = _keyword_classify(message)
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

        # 第二层：小模型流式判断。
        await queue.emit_thinking("🧠 小模型正在分析您的需求...", step="classify")

        candidate: Optional[ClassificationResult] = None
        try:
            full_content = ""
            with timed_span(
                "model.classify_small",
                trace_id=getattr(queue, "trace_id", ""),
            ) as measurement:
                async for chunk in self.chain.astream({
                    "message": message,
                    "context": self._format_context(history),
                }):
                    full_content += chunk.content
                    measurement.add_attributes(extract_model_usage(chunk))

            try:
                data = _parse_json_response(full_content)
                candidate = self._from_data(data)
                result = candidate
                method = "llm"

                if candidate.confidence < LOW_CONFIDENCE_THRESHOLD:
                    await queue.emit_thinking(
                        "🧠 分类置信度偏低，主模型正在仲裁...",
                        step="classify",
                    )
                    try:
                        result = await self._arbitrate_async(
                            message,
                            history,
                            candidate,
                            mode="low_confidence",
                            trace_id=getattr(queue, "trace_id", ""),
                        )
                        method = "llm_arbitration"
                    except Exception as arbiter_error:
                        log_exception(
                            "classifier.arbiter.streaming",
                            arbiter_error,
                            {"message": message},
                        )
                        result = candidate.model_copy(update={
                            "reason": candidate.reason + "；主模型仲裁失败，保留低置信分类",
                        })
                        method = "llm_low_confidence"
                        await queue.emit(EventType.ERROR, {
                            "message": "分类主模型仲裁失败，已保留低置信分类",
                        }, step="classify")
            except ClassificationResponseError as response_error:
                candidate = None
                await queue.emit_thinking(
                    "🧠 分类输出格式异常，主模型正在修复...",
                    step="classify",
                )
                result = await self._arbitrate_async(
                    message,
                    history,
                    candidate=None,
                    mode="repair",
                    error=str(response_error),
                    trace_id=getattr(queue, "trace_id", ""),
                )
                method = "llm_repair"

            classification_payload = {
                "route": result.route,
                "intent": result.intent,
                "sub_intent": result.sub_intent,
                "retrieval": result.retrieval,
                "confidence": result.confidence,
                "reason": result.reason,
                "method": method,
            }
            if len(result.effective_intents) > 1:
                classification_payload.update({
                    "intents": result.effective_intents,
                    "intent_scores": result.intent_scores,
                    "primary_intent": result.intent,
                })
            await queue.emit(EventType.CLASSIFY, classification_payload, step="classify")

            return result
        except Exception as e:
            log_exception("classifier.streaming", e, {"message": message})
            fallback = self._fallback(str(e))
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
