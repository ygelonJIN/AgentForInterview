"""意图打分与路由判定。

取代"数关键词个数"的做法：按词类给不同权重，做跨度去重（"跑步鞋"只算一次），
识别否定（"不要帐篷"），并把命中词作为证据带出去，方便解释和调参。

仍然是确定性的、零模型调用；模糊边界照旧交给小模型。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from app.nlu import lexicon

TRAVEL_DURATION = re.compile(
    r"(?:玩|游|待|停留|去)\s*(?:\d+|[一两二三四五六七八九十])\s*(?:天|日|周|星期|礼拜)"
    r"|\d+\s*(?:天|日)\s*(?:游|行程|攻略)"
)
# 不带动词的时长（"三亚五天"），只作为弱证据
BARE_DURATION = re.compile(r"(?<![今明后昨前第])(?:\d+|[一两二三四五六七八九十]+)\s*(?:天|日|晚)")

# 出行结构：动词必须紧挨着地名表里的词，才算一次出行。
# 以前是裸"去/到"字面包含——"寄到广州""送到汕头"里的"到"也算，
# 于是一句售后问题被加权成旅行意图。这里刻意不含"到"：中文里太泛
# （寄到/送到/发到/运到/搬到/转到/拿到/看到），宁可交给小模型。
_DEPART_VERBS = ("出发去", "动身去", "前往", "抵达", "去", "飞", "回")
_PLACE_WORDS = tuple(sorted({*lexicon.PLACES, *lexicon.PROVINCES}, key=len, reverse=True))
_PLACE_ALT = "|".join(re.escape(word) for word in _PLACE_WORDS)
_VERB_ALT = "|".join(re.escape(word) for word in _DEPART_VERBS)
_TRAVEL_STRUCT = re.compile(
    rf"(?:{_VERB_ALT})\s*(?:{_PLACE_ALT})"      # 去杭州 / 飞三亚 / 回汕头
    rf"|(?:{_PLACE_ALT})\s*(?:出发|玩|逛|游)"    # 杭州出发 / 三亚玩两天
)

SHOPPING_THRESHOLD = 1.2
TRAVEL_THRESHOLD = 1.2
SERVICE_THRESHOLD = 1.2

# 权重：品类名词最硬，动作次之，泛化词最弱
_W_PRODUCT = 1.0
_W_ACTION = 0.6
_W_WEAK = 0.4
_W_TRAVEL = 1.0
_W_SCENE = 0.6
# 地名只证明"提到了地方"，不证明"要出行"。1.0 时两个地名就过线，
# 一句含两个城市的非旅行请求会拿到 0.85 置信度并直接短路小模型。
_W_PLACE = 0.8
_W_DURATION = 1.0
_W_BARE_DURATION = 0.6
_W_CONTEXT = 0.4
_W_SERVICE = 1.0
_W_SERVICE_STRONG = 1.3


@dataclass
class IntentScores:
    scores: Dict[str, float] = field(default_factory=dict)
    hits: Dict[str, List[str]] = field(default_factory=dict)
    weak_hits: Dict[str, List[str]] = field(default_factory=dict)
    product_hits: List[str] = field(default_factory=list)
    route: str = "react"
    # ---- 证据分项：决定"关键词层能不能直接定案" ----
    shopping_actions: List[str] = field(default_factory=list)
    travel_strong_words: List[str] = field(default_factory=list)
    travel_scenes: List[str] = field(default_factory=list)
    travel_places: List[str] = field(default_factory=list)
    travel_has_duration: bool = False
    # 地名以外的出行证据：强旅游词 / 场景词 / 时长
    travel_evidence: bool = False
    # 地名 + 出行结构（"去杭州"/"杭州出发"）：只有地名不算"要出行"
    travel_context: bool = False

    @property
    def shopping(self) -> float:
        return self.scores.get("shopping", 0.0)

    @property
    def travel(self) -> float:
        return self.scores.get("travel", 0.0)

    @property
    def service(self) -> float:
        return self.scores.get("customer_service", 0.0)

    @property
    def shopping_ready(self) -> bool:
        """购物必须有实义品类/品牌词；光说"价格多少钱"不能判购物。"""
        return bool(self.product_hits) and self.shopping >= SHOPPING_THRESHOLD

    @property
    def travel_ready(self) -> bool:
        """地名只证明"提到了地方"，不证明"要出行"。

        以前地名 1.0×2 就过线，一句"上海和汕头哪个好玩"直接定案，
        小模型看不到；含两个城市的非旅行请求也会被判成 travel。
        现在地名权重降到 0.8，且只有地名时必须配出行结构（"去杭州"）。
        """
        if self.travel < TRAVEL_THRESHOLD:
            return False
        if self.travel_evidence:
            return True
        return self.travel_context

    @property
    def service_ready(self) -> bool:
        return bool(self.hits.get("customer_service")) and self.service >= SERVICE_THRESHOLD

    # ---- 强证据：关键词层的"可以直接定案"口径 ----
    # 关键词层只是快路径，不是判断者。证据不够硬就必须交小模型，
    # 否则错判没有任何地方能纠正（模型看不到、日志里 method=keyword 一片正常）。

    @property
    def shopping_strong(self) -> bool:
        """品类/品牌 + 购买动作或价格词。"只报了品类名"不算。"""
        return bool(self.product_hits) and bool(
            self.shopping_actions or self.weak_hits.get("shopping")
        )

    @property
    def travel_strong(self) -> bool:
        """强旅游词，或 地名 + 时长/出行结构。只有地名不算。"""
        if self.travel_strong_words:
            return True
        return bool(self.travel_places) and (
            self.travel_context or self.travel_has_duration
        )

    @property
    def service_strong(self) -> bool:
        """客服是封闭小词表，凑够两个词（或一个强词）就够硬。"""
        return self.service_ready

    @property
    def strong_intents(self) -> List[str]:
        found: List[str] = []
        if self.shopping_ready and self.shopping_strong:
            found.append("shopping")
        if self.travel_ready and self.travel_strong:
            found.append("travel")
        if self.service_ready and self.service_strong:
            found.append("customer_service")
        return found


def _alive_terms(text: str, terms) -> List[str]:
    """命中且未被否定的词（"不要帐篷"里的帐篷不算）。"""
    return [
        term
        for term in lexicon.matched_terms(text, terms)
        if not lexicon.is_negated(text, term)
    ]


def score_intents(message: str) -> IntentScores:
    text = str(message or "")
    result = IntentScores(scores={"shopping": 0.0, "travel": 0.0, "customer_service": 0.0, "general": 0.0})

    # ---- 购物 ----
    products = _alive_terms(text, lexicon.PRODUCT_NOUNS) + _alive_terms(text, lexicon.BRANDS)
    actions = _alive_terms(text, lexicon.SHOPPING_ACTIONS)
    weak = _alive_terms(text, lexicon.SHOPPING_WEAK)
    result.product_hits = products
    result.shopping_actions = actions
    result.hits["shopping"] = products + actions
    result.weak_hits["shopping"] = weak
    result.scores["shopping"] = round(
        _W_PRODUCT * len(products) + _W_ACTION * len(actions) + _W_WEAK * len(weak), 2
    )

    # ---- 旅游 ----
    strong = _alive_terms(text, lexicon.TRAVEL_STRONG)
    scenes = _alive_terms(text, lexicon.TRAVEL_SCENE)
    places = _alive_terms(text, lexicon.PLACES)
    travel_hits = places + strong + scenes
    score = _W_PLACE * len(places) + _W_TRAVEL * len(strong) + _W_SCENE * len(scenes)

    has_duration = bool(TRAVEL_DURATION.search(text))
    bare_duration = False
    if has_duration:
        travel_hits.append("<时长>")
        score += _W_DURATION
    elif BARE_DURATION.search(text):
        bare_duration = True
        score += _W_BARE_DURATION
    # 地名 + 出行结构（"去杭州"）比单说地名更像一次出行
    structured = has_travel_structure(text)
    if places and structured:
        score += _W_CONTEXT

    result.hits["travel"] = travel_hits
    result.scores["travel"] = round(score, 2)
    result.travel_strong_words = strong
    result.travel_scenes = scenes
    result.travel_places = places
    result.travel_has_duration = has_duration or bare_duration
    # 地名之外还有没有证据，决定"只有地名"时要不要强求出行结构。
    result.travel_evidence = bool(strong or scenes or has_duration or bare_duration)
    result.travel_context = bool(places and structured)

    # ---- 客服 ----
    # 强词与普通词合并匹配再按权重累加：matched_terms 会按跨度去重，
    # 所以"退款"不会同时算成"退款 + 退"两条。
    service_terms = list(dict.fromkeys(lexicon.STRONG_SERVICE + lexicon.CUSTOMER_SERVICE))
    service = _alive_terms(text, service_terms)
    result.hits["customer_service"] = service
    result.scores["customer_service"] = round(
        sum(
            _W_SERVICE_STRONG if term in lexicon.STRONG_SERVICE else _W_SERVICE
            for term in service
        ),
        2,
    )

    result.route = decide_route(text)
    return result


def decide_route(message: str) -> str:
    """单步还是多步规划：先看规划类词，再看"玩 N 天"这类明确的多日行程。"""
    text = str(message or "")
    if any(term in text for term in lexicon.ROUTE_PLAN):
        return "plan_and_execute"
    if TRAVEL_DURATION.search(text):
        return "plan_and_execute"
    return "react"


# ---------------------------------------------------------------- 兼容旧调用点


def has_travel_structure(message: str) -> bool:
    """动词/动作是否紧挨着地名表里的词（"去杭州""杭州出发"）。

    只认结构，不认单个字：句子里的"到"可能属于"寄到/送到"，那是物流不是出行。
    """
    return bool(_TRAVEL_STRUCT.search(str(message or "")))


def has_travel_signal(message: str) -> bool:
    text = str(message or "")
    return (
        bool(lexicon.matched_terms(text, lexicon.TRAVEL_STRONG))
        or bool(lexicon.matched_terms(text, lexicon.TRAVEL_SCENE))
        or bool(lexicon.matched_terms(text, lexicon.PLACES))
        or bool(TRAVEL_DURATION.search(text))
        or bool(BARE_DURATION.search(text))
    )


def has_strong_shopping_signal(message: str) -> bool:
    text = str(message or "")
    return bool(
        _alive_terms(text, lexicon.PRODUCT_NOUNS) + _alive_terms(text, lexicon.BRANDS)
    )


def mentions_product_target(message: str) -> bool:
    return has_strong_shopping_signal(message)


def has_purchase_verb(message: str) -> bool:
    return any(verb in str(message or "") for verb in lexicon.PURCHASE_VERBS)


def mentions_owned_item(message: str) -> bool:
    text = str(message or "")
    return any(prefix in text for prefix in lexicon.OWNED_PREFIXES)


def has_task_switch_signal(message: str) -> bool:
    return (
        has_strong_shopping_signal(message)
        or has_travel_signal(message)
        or bool(lexicon.matched_terms(str(message or ""), lexicon.CUSTOMER_SERVICE))
    )
