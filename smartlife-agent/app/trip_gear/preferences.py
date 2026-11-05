"""从长期记忆里抽选品偏好：品牌、住宿习惯。

只做**软提示**：有品牌偏好时同价下优先它，没有就按价格走。偏好永远不能
压过硬约束（预算、规格、可履约），否则就是拿记忆去说服用户买贵的。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

# 只在商品库里出现过、或用户常提的品牌。宁可少认，不要瞎认。
KNOWN_BRANDS = (
    "Salomon", "Osprey", "始祖鸟", "牧高笛", "黑冰", "火枫", "挪客",
    "BlackDiamond", "波司登", "优衣库", "海澜之家", "Levi's", "ZARA", "H&M",
    "MaxMara", "Nike", "Adidas", "Lululemon", "Asics", "迪卡侬",
    "安耐晒", "膳魔师", "小米", "华为", "Apple", "联想",
)

_LODGING_HINTS = {
    "hotel": ("民宿", "酒店", "宾馆", "客栈"),
    "camp": ("露营", "帐篷过夜", "扎营"),
}


@dataclass
class Preferences:
    brands: List[str] = field(default_factory=list)
    lodging: str = ""            # 只是倾向，不覆盖用户本轮明说的住宿方式
    notes: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.brands or self.lodging)

    def as_dict(self) -> Dict[str, Any]:
        return {"brands": list(self.brands), "lodging": self.lodging, "notes": list(self.notes)}


def _content_of(memory: Any) -> str:
    if isinstance(memory, dict):
        return str(memory.get("content") or memory.get("text") or "")
    return str(memory or "")


def extract_preferences(memories: Iterable[Any]) -> Preferences:
    """扫一遍记忆文本，认出品牌与住宿倾向。认不出的东西不猜。"""
    blob = " ".join(_content_of(memory) for memory in memories or [])
    prefs = Preferences()
    if not blob.strip():
        return prefs

    for brand in KNOWN_BRANDS:
        if brand in blob and brand not in prefs.brands:
            prefs.brands.append(brand)

    for value, hints in _LODGING_HINTS.items():
        if any(hint in blob for hint in hints):
            prefs.lodging = value
            break

    if prefs.brands:
        prefs.notes.append("品牌偏好：" + "、".join(prefs.brands[:3]))
    if prefs.lodging:
        prefs.notes.append("住宿倾向：" + ("民宿/酒店" if prefs.lodging == "hotel" else "露营"))
    return prefs


def price_hint(memories: Iterable[Any]) -> str:
    """抓"预算 1500 左右"这类价位习惯，只用于文案说明，不做硬约束。"""
    blob = " ".join(_content_of(memory) for memory in memories or [])
    match = re.search(r"(?:预算|价位|一般花|通常花)[^\d]{0,6}(\d{3,6})", blob)
    return match.group(1) if match else ""
