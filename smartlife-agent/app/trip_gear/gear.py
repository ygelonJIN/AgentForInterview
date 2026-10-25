"""装备派生：只看整趟事实和活动文字，不给行程条目打标签。

规则保持十来条，按属性触发；新场景只需要模型把活动写清楚，不需要改规则表。
"""
from __future__ import annotations

import math
import re
from typing import Dict, List

from app.trip_gear.itinerary import activities_text
from app.trip_gear.models import GearItem, Itinerary, TripFacts

# 每次都会带、平台通常不卖的底座
BASE_ITEMS = (
    ("身份证", "证件"),
    ("手机", "通讯"),
    ("充电器", "电子"),
    ("充电宝", "电子"),
    ("常用药", "药品"),
    ("洗漱用品", "洗护"),
    ("纸巾", "日用"),
    ("钱包", "日用"),
    ("钥匙", "日用"),
    ("口罩", "日用"),
    ("眼镜", "日用"),
    ("水杯", "日用"),
    ("护肤品", "洗护"),
    ("垃圾袋", "日用"),
)

SELF_PROVIDED = {name for name, _ in BASE_ITEMS} | {"钥匙", "雨具"}

# 预算紧张时优先砍掉的可选装备
OPTIONAL = {
    "防晒霜", "帽子", "墨镜", "洗衣袋", "营地灯", "登山杖",
    "防水袋", "液体分装瓶", "速干衣",
}

_WATER_WORDS = ("游泳", "浮潜", "皮划艇", "漂流", "海边", "沙滩", "潜水")
_FORMAL_WORDS = ("见客户", "商务", "正式", "会议", "面试", "婚礼", "晚宴", "汇报", "签约")
_HIKE_WORDS = ("徒步", "登山", "爬山", "越野")
_COLD_THRESHOLD = 15


def _append(reason: str, extra: str) -> str:
    if not extra:
        return reason
    if not reason:
        return extra
    if extra in reason:
        return reason
    return f"{reason}；{extra}"


def derive_gear(
    facts: TripFacts,
    itinerary: Itinerary,
    extra_text: str = "",
) -> List[GearItem]:
    """按整趟事实推导装备清单；extra_text 用于保留用户原话里的场景词。"""
    text = f"{activities_text(itinerary)} {facts.weather_summary} {extra_text}"
    items: Dict[str, GearItem] = {}

    def add(name: str, qty: int = 1, reason: str = "") -> None:
        qty = max(1, int(qty or 1))
        if name in items:
            existing = items[name]
            existing.qty = max(existing.qty, qty)
            existing.reason = _append(existing.reason, reason)
            return
        items[name] = GearItem(name=name, qty=qty, reason=reason)

    if facts.is_camping:
        add("帐篷", math.ceil(facts.people / 2), f"露营过夜，{facts.people} 人")
        add("睡袋", facts.people, "露营过夜")
        add("防潮垫", facts.people, "露营过夜")
        add("头灯", facts.people, "营地夜间照明")

    if facts.min_temp_c is not None and facts.min_temp_c <= _COLD_THRESHOLD:
        add("保暖层", 1, f"夜间最低 {facts.min_temp_c:.0f}℃")
        if "睡袋" in items:
            items["睡袋"].reason = (
                f"露营过夜，夜间 {facts.min_temp_c:.0f}℃，"
                f"温标 ≤ {facts.min_temp_c - 5:.0f}℃"
            )

    if facts.rain_probability is not None and facts.rain_probability >= 40:
        add("雨具", 1, f"降水概率 {facts.rain_probability:.0f}%")
        add("防水鞋", 1, f"降水概率 {facts.rain_probability:.0f}%")

    if (facts.min_temp_c is not None and facts.min_temp_c >= 26) or any(
        word in text for word in _WATER_WORDS
    ):
        add("防晒霜", 1, "户外日晒")
        add("帽子", 1, "户外日晒")
        add("墨镜", 1, "户外日晒")

    if any(word in text for word in _WATER_WORDS):
        add("泳衣", 1, "水上活动")
        add("速干衣", 1, "水上活动")
        add("防水袋", 1, "水上活动")

    if any(word in text for word in _FORMAL_WORDS):
        add("正装", 1, "行程含正式场合")
        add("皮鞋", 1, "行程含正式场合")

    if (facts.days or 0) >= 3:
        add("换洗衣物", max(1, int(facts.days) - 1), f"{facts.days} 天行程，按天数−1 准备")
        add("洗衣袋", 1, "长途换洗")

    if facts.with_kids:
        add("儿童衣物", 1, "同行有儿童")
        add("儿童常用药", 1, "同行有儿童")
        add("湿巾", 1, "同行有儿童")

    if facts.transport == "flight":
        add("液体分装瓶", 1, "乘机随身液体限制")

    if any(word in text for word in _HIKE_WORDS):
        add("登山鞋", 1, "徒步/登山")
        add("双肩包", 1, "徒步/登山")

    for name, reason in BASE_ITEMS:
        add(name, 1, f"通用（{reason}）")

    for item in items.values():
        item.status = "self_provided" if item.name in SELF_PROVIDED else "need_buy"

    return list(items.values())


def _same_name(left: str, right: str) -> bool:
    a = re.sub(r"\s+", "", str(left or ""))
    b = re.sub(r"\s+", "", str(right or ""))
    if not a or not b:
        return False
    return a == b or (len(a) >= 2 and len(b) >= 2 and (a in b or b in a))


def merge_suggestions(
    gear: List[GearItem],
    suggestions: List[GearItem],
) -> List[GearItem]:
    """把模型补充的装备并进规则清单：同名合并，不同名作为长尾补进来。"""
    for suggestion in suggestions or []:
        name = str(suggestion.name or "").strip()
        if len(name) < 2:
            continue
        match = next((item for item in gear if _same_name(item.name, name)), None)
        if match:
            match.qty = max(match.qty, max(1, int(suggestion.qty or 1)))
            match.reason = _append(match.reason, suggestion.reason or "模型补充")
            continue
        status = "self_provided" if name in SELF_PROVIDED else "need_buy"
        gear.append(
            GearItem(
                name=name,
                qty=max(1, int(suggestion.qty or 1)),
                reason=suggestion.reason or "模型补充",
                status=status,
            )
        )
    return gear
