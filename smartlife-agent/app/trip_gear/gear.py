"""装备派生：只看整趟事实和活动文字，不给行程条目打标签。

**只派生"库里可能有"的具体商品词**，不发明内部叫法。
以前这里写的是"保暖层""换洗衣物""防水鞋"这类系统内部词，需要一张
别名表才能对到商品；商品词一多就维护不动。现在直接写用户会说、
库里也真有的词（羽绒服 / T恤 / 登山鞋 / 雨伞 / 儿童…）。

库里没有的（西装、皮鞋、帽子、墨镜…）照样会派生出来，但检索不到就会被
`GearShopper.fill` 丢掉，不会出现在清单里——"有什么才推荐什么"。
"""
from __future__ import annotations

import math
from typing import Dict, List

from app.trip_gear.itinerary import activities_text
from app.trip_gear.models import GearItem, Itinerary, TripFacts
from app.trip_gear.shopping import canonical_gear_name

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

    people = facts.effective_people
    if facts.is_camping:
        add("帐篷", math.ceil(people / 2), f"露营过夜，{people} 人")
        add("睡袋", people, "露营过夜")
        add("防潮垫", people, "露营过夜")
        add("头灯", people, "营地夜间照明")

    if facts.min_temp_c is not None and facts.min_temp_c <= _COLD_THRESHOLD:
        add("羽绒服", 1, f"夜间最低 {facts.min_temp_c:.0f}℃")
        if "睡袋" in items:
            items["睡袋"].reason = (
                f"露营过夜，夜间 {facts.min_temp_c:.0f}℃，"
                f"温标 ≤ {facts.min_temp_c - 5:.0f}℃"
            )

    if facts.rain_probability is not None and facts.rain_probability >= 40:
        add("雨伞", 1, f"降水概率 {facts.rain_probability:.0f}%")
        add("登山鞋", 1, f"降水概率 {facts.rain_probability:.0f}%")

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
        add("西装", 1, "行程含正式场合")
        add("皮鞋", 1, "行程含正式场合")

    if (facts.days or 0) >= 3:
        add("T恤", max(1, int(facts.days) - 1), f"{facts.days} 天行程，按天数−1 准备")
        add("洗衣袋", 1, "长途换洗")

    if facts.with_kids:
        add("儿童", 1, "同行有儿童")
        add("湿巾", 1, "同行有儿童")

    if facts.transport == "flight":
        add("液体分装瓶", 1, "乘机随身液体限制")

    if any(word in text for word in _HIKE_WORDS):
        add("登山鞋", 1, "徒步/登山")
        add("背包", 1, "徒步/登山")

    return list(items.values())


def _same_name(left: str, right: str) -> bool:
    import re

    a = re.sub(r"\s+", "", str(left or ""))
    b = re.sub(r"\s+", "", str(right or ""))
    if not a or not b:
        return False
    return a == b or (len(a) >= 2 and len(b) >= 2 and (a in b or b in a))


def merge_suggestions(
    gear: List[GearItem],
    suggestions: List[GearItem],
) -> List[GearItem]:
    """把模型补充的装备并进规则清单：同名合并，不同名作为长尾补进来。

    长尾项同样要过检索：库里没有就不会出现在最终清单里。
    """
    for suggestion in suggestions or []:
        name = str(suggestion.name or "").strip()
        if len(name) < 2:
            continue
        canonical = canonical_gear_name(name)
        match = next(
            (
                item for item in gear
                if _same_name(item.name, name)
                or (canonical and canonical_gear_name(item.name) == canonical)
            ),
            None,
        )
        if match:
            match.qty = max(match.qty, max(1, int(suggestion.qty or 1)))
            match.reason = _append(match.reason, suggestion.reason or "模型补充")
            continue
        gear.append(
            GearItem(
                name=name,
                qty=max(1, int(suggestion.qty or 1)),
                reason=suggestion.reason or "模型补充",
            )
        )
    return gear
