"""已有装备判定：只认明确表述，并保留出处。

- “我有帐篷”“上周买了睡袋” -> 已有
- “我想买帐篷”“需要买睡袋” -> 未拥有
- 记忆里的“用户买了帐篷”同样算已有
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List

from app.trip_gear.models import GearItem, OwnedItem

_OWNED_RE = re.compile(
    r"(?:带了|带的有|买了|买过|购入了|准备了|(?<![没不缺少无])有)\s*([^，。；;！？\n]{1,40})"
)
_SPLIT_RE = re.compile(r"[、,，和及与]|还有|以及")
_QTY_RE = re.compile(r"^\s*([一两二三四五六七八九十\d]+)\s*(个|顶|双|件|套|条|台|把|瓶|盒)")
_CN_NUM = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_GENERIC = {"装备", "东西", "物品", "这些", "那些", "很多", "一些", "别的"}


def _clean_name(name: str) -> str:
    cleaned = str(name or "").strip()
    cleaned = re.sub(r"^(?:我的|自己的)", "", cleaned)
    return cleaned.strip(" 的了")


def _parse_item(raw: str) -> tuple[str, int]:
    text = str(raw or "").strip()
    qty = 1
    match = _QTY_RE.match(text)
    if match:
        raw_qty = match.group(1)
        if raw_qty.isdigit():
            qty = max(1, int(raw_qty))
        elif raw_qty in _CN_NUM:
            qty = _CN_NUM[raw_qty]
        text = text[match.end():].strip()
    return _clean_name(text), qty


def extract_owned(text: str, source: str = "对话") -> List[OwnedItem]:
    """从一段文字中抽取明确拥有的物品。"""
    found: List[OwnedItem] = []
    for match in _OWNED_RE.finditer(str(text or "")):
        captured = match.group(1)
        for part in _SPLIT_RE.split(captured):
            name, qty = _parse_item(part)
            if len(name) < 2 or name in _GENERIC:
                continue
            # "一堆装备""那些东西"是泛称，没有可购买的具体品类，跳过并留给确认入口。
            if name.endswith(("装备", "东西", "物品", "用品", "家伙什")):
                continue
            found.append(OwnedItem(name=name, qty=qty, source=source))
    return _dedupe(found)


# 说了"有"但没说是哪种东西的表述：不能当成已有（会漏买），也不能当没说（会重复买）。
_AMBIGUOUS_RE = re.compile(
    r"(?:我|自己|家里)(?:都|已经|基本|差不多)?(?:有|带了|买了|备了)"
    r"(?:一些|一堆|很多|不少|点|点儿|几个)?(?:的)?"
    r"[^，。！？,\s]{0,6}?(?:装备|东西|物品|用品|家伙什|这些|那些)"
)
_AMBIGUOUS_READY_RE = re.compile(
    r"(?:装备|东西|物品|行头)(?:都|已经|基本)?(?:齐了|全了|准备好了|够了|有了)"
)


def find_ambiguous_owned(text: str) -> List[str]:
    """找出"我有装备"这类模糊表述，交给用户确认具体是什么。

    抽取器只接受明确名称，模糊表述一律跳过；但直接丢掉会让用户以为系统
    已经记住了，于是重复购买。所以单独收集，在结果里给出确认入口。
    """
    raw = str(text or "")
    found: List[str] = []
    for match in _AMBIGUOUS_RE.finditer(raw):
        phrase = match.group(0).strip()
        if phrase and phrase not in found:
            found.append(phrase)
    for match in _AMBIGUOUS_READY_RE.finditer(raw):
        phrase = match.group(0).strip()
        if phrase and phrase not in found:
            found.append(phrase)
    return found


def extract_from_memories(memories: Iterable[Any]) -> List[OwnedItem]:
    """把长期记忆文本里明确写到的物品并进来。"""
    found: List[OwnedItem] = []
    for memory in memories or []:
        if isinstance(memory, dict):
            content = str(memory.get("content") or memory.get("text") or "")
            metadata = memory.get("metadata") or {}
            memory_id = metadata.get("memory_id") or memory.get("id")
            source = f"记忆:{str(memory_id)[:8]}" if memory_id else "记忆"
        else:
            content = str(memory or "")
            source = "记忆"
        if content:
            found.extend(extract_owned(content, source=source))
    return _dedupe(found)


def _dedupe(items: List[OwnedItem]) -> List[OwnedItem]:
    merged: dict[str, OwnedItem] = {}
    for item in items:
        existing = merged.get(item.name)
        if existing:
            existing.qty = max(existing.qty, item.qty)
        else:
            merged[item.name] = item
    return list(merged.values())


def merge_owned(*groups: Iterable[OwnedItem]) -> List[OwnedItem]:
    """合并多段来源（本轮、上文、记忆），同名取最大数量。"""
    return _dedupe([item for group in groups for item in (group or [])])


def _names_match(owned: str, gear: str) -> bool:
    left = re.sub(r"\s+", "", owned)
    right = re.sub(r"\s+", "", gear)
    if len(left) < 2 or len(right) < 2:
        return False
    return left in right or right in left


def apply_owned(gear: List[GearItem], owned: List[OwnedItem]) -> List[GearItem]:
    """把已有物品映射回装备清单；数量不够时只补缺口。"""
    for item in gear:
        for own in owned:
            if not _names_match(own.name, item.name):
                continue
            if own.qty >= item.qty:
                item.status = "owned"
                item.reason = _append(item.reason, f"已有（{own.source}）")
            else:
                item.qty -= own.qty
                item.reason = _append(
                    item.reason,
                    f"已有 {own.qty} 件（{own.source}），还差 {item.qty} 件",
                )
            break
    return gear


def _append(reason: str, extra: str) -> str:
    if not extra:
        return reason
    if not reason:
        return extra
    if extra in reason:
        return reason
    return f"{reason}；{extra}"
