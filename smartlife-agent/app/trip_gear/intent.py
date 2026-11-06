"""识别"这次出行要带什么"的请求。

只做一件事：判断一句话是否应该进入 trip_gear 链路。不调模型。

规则不再是"出现户外词就算"，而是三条一起看：
1. 有购买动作（买/推荐/搜）→ 不进装备链路，走购物或复合意图；
2. 出现装备词（装备/清单/带什么/行李）→ 进；
3. 只有场景词（露营/出差/度假）时，还必须有出行语境（去/到/周末/天数）。
"""
from __future__ import annotations

from app.nlu import lexicon, signals

_TRAVEL_CONTEXT_EXTRA = ("去", "到", "周末", "假期", "放假", "长假", "小长假", "出差", "度假")


def _has_purchase_action(text: str) -> bool:
    return any(verb in text for verb in lexicon.PURCHASE_VERBS)


def _has_travel_context(text: str) -> bool:
    return bool(
        lexicon.matched_terms(text, lexicon.TRAVEL_STRONG)
        or lexicon.matched_terms(text, lexicon.PLACES)
        or any(word in text for word in _TRAVEL_CONTEXT_EXTRA)
        or signals.TRAVEL_DURATION.search(text)
        or signals.BARE_DURATION.search(text)
    )


def is_gear_request(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if _has_purchase_action(text):
        return False
    if lexicon.matched_terms(text, lexicon.GEAR_TERMS):
        return True
    if lexicon.matched_terms(text, lexicon.TRAVEL_SCENE):
        return _has_travel_context(text)
    return False
