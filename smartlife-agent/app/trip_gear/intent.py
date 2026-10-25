"""识别“这次出行要带什么”的请求。

只做一件事：判断一句话是否应该进入 trip_gear 链路。
不依赖模型，避免为了路由多调一次 LLM。
"""
from __future__ import annotations

_TRAVEL_SIGNALS = (
    "去", "到", "旅行", "旅游", "出差", "度假", "周末", "假期",
    "行程", "几天", "天", "晚",
)

_GEAR_SIGNALS = (
    "装备", "清单", "带什么", "带啥", "穿什么", "准备什么",
    "要带", "该带", "行李", "行囊",
)

_OUTDOOR_SIGNALS = (
    "露营", "营地", "徒步", "登山", "爬山", "自驾", "滑雪",
    "骑行", "海岛", "亲子游", "溯溪",
)

# 这些场景不一定有“装备”二字，但衣物/正装/防晒本身就是装备
_SCENE_SIGNALS = (
    "出差", "见客户", "商务", "会议", "面试", "婚礼", "晚宴",
    "度假", "海边", "沙滩",
)

_BUY_SIGNALS = ("买", "购买", "下单", "推荐")


def is_gear_request(message: str) -> bool:
    """判断是否是需要“行程 + 装备清单”的请求。

    规则：
    1. 必须有出行语境（去/到/几天/周末……）；
    2. 出现装备/清单/带什么等词 -> 是；
    3. 出现露营/徒步等强场景词，且不是在问“买什么” -> 是。
    """
    text = str(message or "")
    if not text.strip():
        return False
    if not any(word in text for word in _TRAVEL_SIGNALS):
        return False
    if any(word in text for word in _GEAR_SIGNALS):
        return True
    if any(word in text for word in _OUTDOOR_SIGNALS + _SCENE_SIGNALS) and not any(
        word in text for word in _BUY_SIGNALS
    ):
        return True
    return False
