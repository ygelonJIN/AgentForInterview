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

SHOPPING_THRESHOLD = 1.2
TRAVEL_THRESHOLD = 1.2
SERVICE_THRESHOLD = 1.2

# 权重：品类名词最硬，动作次之，泛化词最弱
_W_PRODUCT = 1.0
_W_ACTION = 0.6
_W_WEAK = 0.4
_W_TRAVEL = 1.0
_W_SCENE = 0.6
_W_PLACE = 1.0
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
        return bool(self.hits.get("travel")) and self.travel >= TRAVEL_THRESHOLD

    @property
    def service_ready(self) -> bool:
        return bool(self.hits.get("customer_service")) and self.service >= SERVICE_THRESHOLD


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
    result.hits["shopping"] = products + actions
    result.weak_hits["shopping"] = weak
    result.scores["shopping"] = round(
        _W_PRODUCT * len(products) + _W_ACTION * len(actions) + _W_WEAK * len(weak), 2
    )

    # ---- 旅游 ----
    strong = _alive_terms(text, lexicon.TRAVEL_STRONG)
    scenes = _alive_terms(text, lexicon.TRAVEL_SCENE)
    places = _alive_terms(text, lexicon.PLACES)
    contexts = _alive_terms(text, lexicon.TRAVEL_CONTEXT)
    travel_hits = places + strong + scenes
    score = _W_PLACE * len(places) + _W_TRAVEL * len(strong) + _W_SCENE * len(scenes)

    if TRAVEL_DURATION.search(text):
        travel_hits.append("<时长>")
        score += _W_DURATION
    elif BARE_DURATION.search(text):
        score += _W_BARE_DURATION
    # 地名 + 出行语境（"去杭州"）比单说地名更像一次出行
    if places and contexts:
        score += _W_CONTEXT

    result.hits["travel"] = travel_hits
    result.scores["travel"] = round(score, 2)

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
