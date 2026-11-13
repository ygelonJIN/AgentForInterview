"""
统一分类器 - 合并 Router + Intent 为一次小模型调用
减少一次 LLM 调用，延迟降低 1-2 秒

输出结构：
{
    "route": "react" | "plan_and_execute",
    "intent": "shopping" | "travel" | "customer_service" | "general",
    "intents": ["shopping", "travel"],  # 明确复合任务可保留 1-2 个意图
    "sub_intent": "search" | "recommend" | "order_status" | ...,
    "retrieval": "sql_only" | "rag_only" | "mixed" | "none",
    "confidence": 0.0-1.0,
    "reason": "路由理由"
}
"""
import json
from app.streaming import INTENT_LABELS, EventType
import re
from typing import Dict, Any, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_llm, create_small_llm
from app.nlu import signals
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
    # 意图不再是写死的四个值：导入的 Agent 可以声明新的意图入口
    # （app/agents/specs.py 的 AgentSpec.intent），校验在模型层统一做。
    intent: str = Field(
        default="general",
        description="主意图类别；兼容旧的单意图字段"
    )
    intents: List[str] = Field(
        default_factory=list,
        description="明确的多意图集合；为空时等价于 [intent]"
    )
    primary_intent: Optional[str] = Field(
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
        allowed = _allowed_intents()
        unknown = [item for item in self.intents if item not in allowed]
        if unknown:
            raise ValueError(f"未知意图: {unknown}；可用 {sorted(allowed)}")
        return self

    @property
    def effective_intents(self) -> List[str]:
        """兼容旧代码：未显式给出 intents 时退化为单意图。"""
        return list(self.intents or [self.intent])

    @property
    def agent_label(self) -> str:
        return "+".join(self.effective_intents)


# 关键词层：词表和权重都在 app.nlu，这里只负责"够不够硬"的阈值与输出格式。
# 旧实现按"命中几个词"数数，同义词/否定/跨度重复都会算错；现在按加权打分，
# 阈值仍然是确定性的，模糊边界照旧交给小模型。
_TRAVEL_DURATION_RE = signals.TRAVEL_DURATION

_PRIORITY = {"shopping": 0, "travel": 1, "customer_service": 2}


def _allowed_intents() -> set:
    try:
        from app.agents.specs import allowed_intents

        return set(allowed_intents())
    except Exception:
        return {"shopping", "travel", "customer_service", "general"}


def _extra_intents_text() -> str:
    """把导入的 Agent 声明的新意图拼进 prompt；没有新意图时是空行。"""
    base = {"shopping", "travel", "customer_service", "general"}
    lines = []
    try:
        from app.agents.specs import AGENT_SPECS

        for spec in AGENT_SPECS.values():
            intent = str(spec.intent or "")
            if intent and intent not in base and intent != "forced_only":
                lines.append(f'- "{intent}": {spec.display_name}（{spec.description}）')
    except Exception:
        return ""
    return "\n".join(lines)


def _has_purchase_verb(message: str) -> bool:
    return signals.has_purchase_verb(message)


def _mentions_product_target(message: str) -> bool:
    return signals.mentions_product_target(message)


def _mentions_owned_item(message: str) -> bool:
    return signals.mentions_owned_item(message)


def _has_strong_shopping_signal(message: str) -> bool:
    return signals.has_strong_shopping_signal(message)


def _has_travel_signal(message: str) -> bool:
    return signals.has_travel_signal(message)


def _sub_intent_for(intent: str, route: str) -> str:
    if intent == "travel":
        return "plan_trip" if route == "plan_and_execute" else "qa"
    if intent in {"shopping", "customer_service"}:
        return "search"
    return "chat"


# 寒暄与确认词：既不是新任务，也不该触发重算。
_SMALL_TALK = (
    "你好", "您好", "在吗", "谢谢", "多谢", "好的", "好啊", "行", "嗯", "哦",
    "收到", "明白了", "知道了", "ok", "OK", "拜拜", "再见", "辛苦了",
)


# 问句标记：用来区分"补充信息"和"另起一个问题"。
_QUESTION_HINTS = ("?", "？", "吗", "呢", "什么", "怎么", "为什么", "哪", "多少", "如何", "是否")


def _is_bare_question(message: str) -> bool:
    text = str(message or "")
    return any(hint in text for hint in _QUESTION_HINTS)


# 未来日期词：在行程上下文里，这种句子基本就是在补出发时间。
_FUTURE_DATE = re.compile(r"(明天|后天|大后天|下周[一二三四五六日天末]?|下个月|下月|周末|\d+\s*号)")


def _has_domain_evidence(message: str) -> bool:
    """这句话里有没有"属于当前领域"的实词。

    只看实义证据（品类/品牌、地名、强旅游词、场景词、时长、客服词），
    不看"推荐/找"这类泛动作——否则"推荐一部电影"会被当成购物证据，
    于是在行程中途被当成补充信息。
    """
    signal = signals.score_intents(message)
    return bool(
        signal.product_hits
        or signal.travel_places
        or signal.travel_strong_words
        or signal.travel_scenes
        or signal.travel_has_duration
        or signal.hits.get("customer_service")
        or signal.weak_hits.get("shopping")
        or bool(_FUTURE_DATE.search(str(message or "")))
    )


def _is_small_talk(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return True
    if len(text) <= 4 and any(word in text for word in _SMALL_TALK):
        return True
    return text in {word.lower() for word in _SMALL_TALK}


def _keyword_evidence(message: str) -> Optional[Dict[str, Any]]:
    """关键词层给出的候选结论与证据强度。

    关键词层只是快路径，不是判断者：只有**强证据**（口径见
    ``signals.IntentScores.shopping_strong`` 等）才允许直接定案；
    弱证据仍交小模型，只作为线索一起送过去。这样错判至少还有一次
    被模型纠正的机会，而不是静默地成为最终结论。
    """
    signal = signals.score_intents(message)
    route = signal.route
    weak_count = len(signal.weak_hits.get("shopping") or [])

    candidates: Dict[str, float] = {}
    if signal.shopping_ready:
        candidates["shopping"] = signal.shopping
    if signal.travel_ready:
        candidates["travel"] = signal.travel
    if signal.service_ready:
        candidates["customer_service"] = signal.service

    if not candidates:
        return None

    scores = {key: float(value) for key, value in candidates.items()}
    strong = [name for name in candidates if name in signal.strong_intents]
    picked = strong or list(candidates)
    strength = "strong" if strong else "weak"

    # 明确的复合意图：两类都是强证据时保留两个，只有 shopping+travel 并行。
    if len(picked) >= 2:
        ordered = sorted(picked, key=lambda item: (-candidates[item], _PRIORITY[item]))[:2]
        retrieval = "mixed" if "shopping" in ordered else "rag_only"
        sub_intents = [_sub_intent_for(item, route) for item in ordered]
        return {
            "route": route,
            "intent": ordered[0],
            "intents": ordered,
            "intent_scores": scores,
            "sub_intent": "+".join(sub_intents),
            "retrieval": retrieval,
            "confidence": 0.9,
            "reason": "多意图关键词匹配: "
            + ", ".join(f"{key}={candidates[key]:g}" for key in ordered),
            "from": "keyword",
            "strength": strength,
        }

    intent = max(picked, key=lambda item: (candidates[item], -_PRIORITY[item]))
    retrieval = {"shopping": "mixed", "travel": "rag_only", "customer_service": "none"}[intent]
    hits = len(signal.hits.get(intent) or [])
    reason = f"关键词匹配 ({hits} 个关键词命中"
    if weak_count and intent != "shopping":
        reason += f", 含弱购物词 {weak_count}"
    reason += ")"
    # 单意图不额外塞 intents/intent_scores：事件载荷保持与旧版一致，
    # 下游由 ClassificationResult 的校验器补默认值。
    return {
        "route": route,
        "intent": intent,
        "sub_intent": _sub_intent_for(intent, route),
        "retrieval": retrieval,
        "confidence": 0.85,
        "reason": reason,
        "from": "keyword",
        "strength": strength,
    }


def _keyword_classify(message: str) -> Optional[Dict[str, Any]]:
    """关键词层的**结论**：只有强证据才返回，弱证据返回 None（交小模型）。"""
    evidence = _keyword_evidence(message)
    if evidence and evidence.get("strength") == "strong":
        return evidence
    return None


def keyword_display(message: str) -> Dict[str, Any]:
    """关键词层的全部证据，只用于展示，不参与调用链的任何判定。

    以前界面上只有一句"关键词匹配 (3 个关键词命中)"：用户既看不到命中了
    哪几个词，也不知道"够不够硬、为什么没走模型"。这里把命中的词、各意图
    分数、以及"是否达到定案线"一起带出去，过程卡片直接摊开。
    """
    signal = signals.score_intents(message)
    hits = {
        intent: list(words)
        for intent, words in (signal.hits or {}).items()
        if words
    }
    weak = list(signal.weak_hits.get("shopping") or [])
    if weak:
        hits["shopping_weak"] = weak
    return {
        "keyword_hits": hits,
        "keyword_scores": {
            key: float(value) for key, value in (signal.scores or {}).items()
        },
        "keyword_ready": {
            "shopping": bool(signal.shopping_ready),
            "travel": bool(signal.travel_ready),
            "customer_service": bool(signal.service_ready),
        },
    }


def hits_brief(display: Dict[str, Any]) -> str:
    """把证据压成一句人话：``购物 跑鞋/推荐 · 旅行 杭州``。"""
    parts: List[str] = []
    for intent, words in (display.get("keyword_hits") or {}).items():
        if not words:
            continue
        label = "购物弱词" if intent == "shopping_weak" else INTENT_LABELS.get(intent, intent)
        parts.append(f"{label} {'/'.join(str(word) for word in words[:6])}")
    return " · ".join(parts)


def _keyword_hint(evidence: Optional[Dict[str, Any]]) -> str:
    """弱证据作为线索送进小模型 prompt；没有证据时明确写"无"。"""
    if not evidence:
        return "无"
    candidates = "、".join(evidence.get("intents") or [evidence.get("intent", "")])
    return (
        f"{candidates}（{evidence.get('reason', '')}）；"
        "只是线索：与本句实际诉求不符时，按你的判断改"
    )


def _keyword_result(evidence: Dict[str, Any]) -> "ClassificationResult":
    return ClassificationResult(**{
        key: value for key, value in evidence.items()
        if key in ClassificationResult.model_fields
    })


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
- "general": 通用对话
{extra_intents}
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

sub_intent 取值：
- 用户要的是"出行清单/装备清单/带什么东西/行程加装备一起规划"时，sub_intent 必须写 "gear_plan"；
- 其他情况写一句不超过 8 字的简述（例如"商品比价"、"订单售后"）。

严格返回JSON，不要其他文字：
{{"route": "...", "intent": "...", "intents": ["..."], "primary_intent": "...", "intent_scores": {{"...": 0.X}}, "sub_intent": "...", "retrieval": "...", "confidence": 0.X, "reason": "..."}}"""),
            ("user", "对话上下文：{context}\n关键词线索：{keyword_hint}\n\n当前消息：{message}")
])

_CLASSIFY_ARBITER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是分类纠错与仲裁器。独立分析用户当前消息，并检查候选分类是否合理。

只允许输出一个严格 JSON 对象，必须包含：
route、intent、intents、primary_intent、intent_scores、sub_intent、retrieval、confidence、reason。

判定口径（与主分类器一致，不要自行发明）：
- route: 单步查询、导购、客服、闲聊 -> react；多日行程、采购清单、预算分配、跨场景任务、需要用户确认计划 -> plan_and_execute
- intent: 有明确商品品类且要买/比价 -> shopping；有目的地或出行场景 -> travel；订单/退换/物流 -> customer_service；其余 general
- retrieval 只是提示：真正的 SQL/RAG 由证据需求决定，不要为了"看起来完整"硬填 mixed

枚举限制：
- route: react / plan_and_execute
- intent / intents / primary_intent: shopping / travel / customer_service / general
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
            # 不用 with_structured_output：部分 OpenAI 兼容服务不支持 response_format
            # (json_schema) 会直接 400。提示词已要求严格 JSON，输出由 _result_data 解析。
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
    async def _emit_classify_step(
        queue: Any,
        *,
        method: str,
        result: "ClassificationResult",
        display: Optional[Dict[str, Any]] = None,
    ) -> None:
        """把"这一步到底怎么判的"回写到过程卡片的第 1 阶段描述。

        第 1 阶段一开始的文案是通用流程（"关键词层 → 小模型 → 仲裁"），
        真正定案的方法要等分类结束才知道。不回写的话，关键词直通的轮次
        也会显示"小模型分析"，界面在解释一件没发生的事。
        """
        from app.streaming import (
            INTENT_LABELS,
            RETRIEVAL_LABELS,
            RETRIEVAL_SCOPE,
            describe_classification_method,
        )

        intents = result.effective_intents or [result.intent]
        labels = " + ".join(INTENT_LABELS.get(str(item), str(item)) for item in intents)
        retrieval = str(result.retrieval or "")
        scope = RETRIEVAL_SCOPE.get(retrieval, "")
        hit_count = sum(
            len(words) for words in ((display or {}).get("keyword_hits") or {}).values()
        )
        description = (
            f"{describe_classification_method(method)} · 意图 {labels} · "
            f"检索 {RETRIEVAL_LABELS.get(retrieval, retrieval or '——')}"
            f"{f'（{scope}）' if scope else ''} · 关键词命中 {hit_count} 个"
        )
        try:
            await queue.emit(EventType.STEP, {
                "step": 1, "total": 4,
                "name": "统一分类",
                "description": description,
                "status": "completed",
            }, step="classify")
        except Exception:
            pass

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
    def _has_task_switch_signal(message: str, previous_intent: str = "") -> bool:
        """只有**跨领域**的新任务才算切换。

        ``signals.has_task_switch_signal`` 把"任何旅游词"都当新任务，
        于是行程里问一句"酒店怎么选"也会被判成另起一轮。这里按上一轮
        意图比对：还在同一个领域就不算切换。
        """
        signal = signals.score_intents(message)
        shopping = bool(signal.product_hits)
        travel = bool(
            signal.travel_places or signal.travel_strong_words or signal.travel_scenes
        )
        service = bool(signal.hits.get("customer_service"))
        if shopping and previous_intent != "shopping":
            return True
        if travel and previous_intent not in {"travel", "trip_gear"}:
            return True
        if service and previous_intent != "customer_service":
            return True
        return False

    def _context_fallback(
        self,
        message: str,
        previous: Optional[ClassificationResult],
    ) -> Optional[ClassificationResult]:
        """多轮里"没头没尾的一句"沿用上一轮，不要重新开始。

        两种触发：
        - 指代（"这个""刚才""改成"）——本来就有；
        - 行程中途补一句"我有一个 pocket3"——这是**装备信息**，不是新任务。
          以前它会因为关键词层没命中、小模型判成 general，整轮掉进通用助手，
          把已经排好的行程和槽位全丢在一边，还得从头再问一遍。
        """
        if not previous or self._has_task_switch_signal(message, previous.intent):
            return None
        # 寒暄不是新任务，但也不该让行程重算一遍。
        if _is_small_talk(message):
            return None
        # 句子里没有任何本领域的实词（"今天天气怎么样""推荐一部电影"）：
        # 可能是在问别的事，交给小模型判——它能看到上文，判得比规则准。
        # 有实词（地名/品类/预算/日期/客服词）才默认沿用，避免把行程丢在半路。
        # 指代（"就按刚才那个方案"）本身就是延续，不需要再看实词。
        followup = self._uses_reference(message)
        gear_note = (
            previous.intent in {"travel", "trip_gear"}
            and signals.mentions_owned_item(message)
        )
        if not followup and not gear_note and not _has_domain_evidence(message):
            return None

        # 默认沿用：同一段对话里，只有"明确的新任务"才切换 Agent。

        update = {
            "confidence": max(0.55, min(previous.confidence, 0.8)),
            "reason": f"多轮延续，沿用上一轮分类：{previous.intent}/{previous.route}",
        }
        if gear_note and not followup:
            # 已有物品只对装备段有用：直接标 gear_plan，路由到 trip_gear，
            # 而不是再走一遍没有装备段的 travel。
            update.update({
                "sub_intent": "gear_plan",
                "reason": f"多轮补充装备信息，沿用上一轮分类（{previous.intent}）并转装备协同",
            })
        return previous.model_copy(update=update)

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

    @staticmethod
    def _result_data(result: Any) -> Dict[str, Any]:
        if isinstance(result, ClassificationResult):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        # 注意顺序：AIMessage 也是 pydantic 模型（有 model_dump），必须先按消息处理，
        # 否则会把 {content, response_metadata, ...} 当成分类结果，字段全缺。
        content = getattr(result, "content", None)
        if isinstance(content, (str, list)):
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            return _parse_json_response(content)
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return _parse_json_response(str(result))

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
            self._result_data(result),
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
            self._result_data(result),
            reason_default="主模型分类仲裁",
        )

    @staticmethod
    def _fallback(error: str, message: str = "") -> ClassificationResult:
        """分类失败时的确定性兜底：有关键词证据就按关键词走，没有才回 general。"""
        signal = signals.score_intents(message) if message else None
        if signal is not None:
            candidates = []
            if signal.shopping_ready:
                candidates.append(("shopping", signal.shopping))
            if signal.travel_ready:
                candidates.append(("travel", signal.travel))
            if signal.service_ready:
                candidates.append(("customer_service", signal.service))
            if candidates:
                intent = max(candidates, key=lambda item: item[1])[0]
                sub_intent = (
                    "plan_trip"
                    if intent == "travel" and signal.route == "plan_and_execute"
                    else "qa"
                    if intent == "travel"
                    else "search"
                    if intent in {"shopping", "customer_service"}
                    else "chat"
                )
                retrieval = {
                    "shopping": "mixed",
                    "travel": "rag_only",
                    "customer_service": "none",
                }[intent]
                return ClassificationResult(
                    route=signal.route,
                    intent=intent,
                    intents=[intent],
                    intent_scores={intent: candidates[0][1]},
                    sub_intent=sub_intent,
                    retrieval=retrieval,
                    confidence=0.4,
                    reason=f"模型分类失败，按关键词兜底: {error[:50]}",
                )
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
    def _apply_travel_gear_policy(
        classification: ClassificationResult,
        message: str,
    ) -> ClassificationResult:
        """旅游 + 已有物品：走行程装备协同，不拆出无购买动作的购物分支。"""
        if not _has_travel_signal(message):
            return classification
        intents = list(classification.effective_intents)
        dropped = False
        if (
            "shopping" in intents
            and _mentions_product_target(message)
            and not _has_purchase_verb(message)
        ):
            intents = [item for item in intents if item != "shopping"] or ["travel"]
            dropped = True
        owns = _mentions_owned_item(message)
        if not dropped and not (owns and set(intents) <= {"travel"}):
            return classification

        needs_plan = (
            classification.route == "plan_and_execute"
            or bool(_TRAVEL_DURATION_RE.search(message))
            or owns
        )
        return classification.model_copy(update={
            "intent": "travel",
            "intents": intents or ["travel"],
            "route": "plan_and_execute" if needs_plan else classification.route,
            "sub_intent": "gear_plan" if owns else ("plan_trip" if needs_plan else "qa"),
            "retrieval": "rag_only",
            "reason": (
                "旅游 + 已有物品：改走行程装备协同"
                if owns
                else f"旅游场景下忽略无购买动作的商品名词（{classification.intent} -> travel）"
            ),
        })

    @staticmethod
    def apply_scene_constraint(
        classification: ClassificationResult,
        scene: str,
        message: str,
    ) -> ClassificationResult:
        """让 UI 场景成为路由约束，防止通用金额词跨场景误判。"""
        classification = UnifiedClassifier._apply_travel_gear_policy(classification, message)
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
        # 只有强证据才短路；弱证据带进第二层当线索。
        evidence = _keyword_evidence(message)
        if evidence and evidence.get("strength") == "strong":
            return _keyword_result(evidence)

        context_fallback = self._context_fallback(message, previous)
        if context_fallback:
            return context_fallback

        # 第二层：小模型判断；低置信或格式失败时只做一次主模型仲裁。
        candidate: Optional[ClassificationResult] = None
        try:
            result = self.chain.invoke({
                "message": message,
                "keyword_hint": _keyword_hint(evidence),
                "context": self._format_context(history),
                "extra_intents": _extra_intents_text(),
            })
            data = self._result_data(result)
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
                return self._fallback(f"{e}; 仲裁失败: {arbiter_error}", message)

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
        # 只有强证据才短路；弱证据带进第二层当线索。
        evidence = _keyword_evidence(message)
        if evidence and evidence.get("strength") == "strong":
            return _keyword_result(evidence)

        context_fallback = self._context_fallback(message, previous)
        if context_fallback:
            return context_fallback

        # 第二层：小模型异步调用；低置信或格式失败时只做一次主模型仲裁。
        candidate: Optional[ClassificationResult] = None
        try:
            result = await self.chain.ainvoke({
                "message": message,
                "keyword_hint": _keyword_hint(evidence),
                "context": self._format_context(history),
                "extra_intents": _extra_intents_text(),
            })
            data = self._result_data(result)
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
                return self._fallback(f"{e}; 仲裁失败: {arbiter_error}", message)

    async def aclassify_streaming(
        self,
        message: str,
        queue: 'EventSink',
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
        # 只有强证据才短路；弱证据带进第二层当线索，模型可以推翻。
        evidence = _keyword_evidence(message)
        # 关键词证据无论够不够硬都要摊在界面上：用户问"为什么选了这条链路"，
        # 答案在这里，不在模型理由里。
        display = keyword_display(message)
        brief = hits_brief(display)
        if evidence and evidence.get("strength") == "strong":
            await queue.emit_thinking(
                f"关键词层直通（强证据）：{brief}；本轮不调分类模型",
                step="classify",
            )
            keyword_result = _keyword_result(evidence)
            # 先回写步骤描述，再发分类结论：`classify` 事件是这一阶段的收尾，
            # 之后不该再冒出一条阶段事件。
            await self._emit_classify_step(
                queue, method="keyword", result=keyword_result, display=display
            )
            await queue.emit(EventType.CLASSIFY, {
                **evidence,
                **display,
                "method": "keyword"
            }, step="classify")
            return keyword_result

        context_fallback = self._context_fallback(message, previous)
        if context_fallback:
            await queue.emit_thinking(
                f"关键词层未定案（{brief or '无命中'}），沿用上一轮意图",
                step="classify",
            )
            await self._emit_classify_step(
                queue, method="context", result=context_fallback, display=display
            )
            await queue.emit(EventType.CLASSIFY, {
                "route": context_fallback.route,
                "intent": context_fallback.intent,
                "sub_intent": context_fallback.sub_intent,
                "retrieval": context_fallback.retrieval,
                "confidence": context_fallback.confidence,
                "reason": context_fallback.reason,
                "method": "context",
                **display,
            }, step="classify")
            return context_fallback

        # 第二层：小模型流式判断。
        await queue.emit_thinking(
            f"🧠 小模型正在分析您的需求...（关键词层：{brief or '无命中'}）",
            step="classify",
        )

        candidate: Optional[ClassificationResult] = None
        try:
            with timed_span(
                "model.classify_small",
                trace_id=getattr(queue, "trace_id", ""),
            ) as measurement:
                structured_result = await self.chain.ainvoke({
                    "message": message,
                    "context": self._format_context(history),
                    # 提示词里有 {extra_intents}，漏传就等于每次分类都 KeyError；
                    # 流式这条路径一直没传，凡是"关键词没命中"的消息都只能走兜底。
                    "extra_intents": _extra_intents_text(),
                    "keyword_hint": _keyword_hint(evidence),
                })
                measurement.add_attributes(extract_model_usage(structured_result))

            try:
                data = self._result_data(structured_result)
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
                "strength": "weak" if evidence else "none",
                **display,
            }
            if len(result.effective_intents) > 1:
                classification_payload.update({
                    "intents": result.effective_intents,
                    "intent_scores": result.intent_scores,
                    "primary_intent": result.intent,
                })
            await self._emit_classify_step(
                queue, method=method, result=result, display=display
            )
            await queue.emit(EventType.CLASSIFY, classification_payload, step="classify")

            return result
        except Exception as e:
            log_exception("classifier.streaming", e, {"message": message})
            fallback = self._fallback(str(e), message)
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
