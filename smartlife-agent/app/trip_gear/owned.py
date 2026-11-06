"""已有装备判定：只认明确表述，并保留出处。

- “我有帐篷”“上周买了睡袋” -> 已有
- “我想买帐篷”“需要买睡袋” -> 未拥有
- 记忆里的“用户买了帐篷”同样算已有
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List

from app.trip_gear.models import GearItem, OwnedItem
from app.trip_gear.spec_match import parse_specs

_OWNED_RE = re.compile(
    r"(?:带了|带的有|买了|买过|购入了|准备了|(?<![没不缺少无])有)\s*([^，。；;！？\n]{1,40})"
)
_SPLIT_RE = re.compile(r"[、,，和及与]|还有|以及")
_QTY_RE = re.compile(
    r"^\s*([一两二三四五六七八九十\d]+)\s*"
    r"(个|顶|双|件|套|条|台|把|瓶|盒|支|只|张|根|副|包|枚|罐|袋|副|辆|块)"
)
_CN_NUM = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_GENERIC = {"装备", "东西", "物品", "这些", "那些", "很多", "一些", "别的"}


def _clean_name(name: str) -> str:
    cleaned = str(name or "").strip()
    cleaned = re.sub(r"^(?:我的|自己的)", "", cleaned)
    return cleaned.strip(" 的了")


def _parse_item(raw: str) -> tuple[str, int, dict]:
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
    name = _clean_name(text)
    # 名称与描述分开传："-5℃ 的睡袋"要把温度落成温标，而不是只留个名字。
    return name, qty, parse_specs(text, name)


def extract_owned(text: str, source: str = "对话") -> List[OwnedItem]:
    """从一段文字中抽取明确拥有的物品。"""
    found: List[OwnedItem] = []
    for match in _OWNED_RE.finditer(str(text or "")):
        captured = match.group(1)
        for part in _SPLIT_RE.split(captured):
            name, qty, specs = _parse_item(part)
            if len(name) < 2 or name in _GENERIC:
                continue
            # "一堆装备""那些东西"是泛称，没有可购买的具体品类，跳过并留给确认入口。
            if name.endswith(("装备", "东西", "物品", "用品", "家伙什")):
                continue
            found.append(OwnedItem(name=name, qty=qty, source=source, specs=specs))
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
            # 同一件东西可能一次说名字、一次说规格，两处都要留下。
            for key, value in (item.specs or {}).items():
                existing.specs.setdefault(key, value)
        else:
            merged[item.name] = item
    return list(merged.values())


def merge_owned(*groups: Iterable[OwnedItem]) -> List[OwnedItem]:
    """合并多段来源（本轮、上文、记忆），同名取最大数量。"""
    return _dedupe([item for group in groups for item in (group or [])])


def _names_match(owned: str, gear: str) -> bool:
    """只做词形包含判断：不维护"西装外套→正装"这种别名表。

    商品词一多，别名表就维护不动；归属判断交给商品检索（见 GearShopper），
    这里只负责"用户说的是不是同一件东西"。
    """
    left = re.sub(r"\s+", "", owned)
    right = re.sub(r"\s+", "", gear)
    if len(left) < 2 or len(right) < 2:
        return False
    return left in right or right in left


def apply_owned(gear: List[GearItem], owned: List[OwnedItem]) -> List[GearItem]:
    """把已有物品映射回装备清单；数量不够时只补缺口。

    规则推导不出来的"已有"（用户有一件西装外套，但正式场合规则只写"正装"）
    在匹配不上的情况下**补进清单**并标 owned——否则用户说了等于没说，
    界面上永远是"已有 0"。
    """
    matched: set = set()
    for item in gear:
        for index, own in enumerate(owned):
            if not _names_match(own.name, item.name):
                continue
            matched.add(index)
            if own.qty >= item.qty:
                item.status = "owned"
                # 规格必须跟着走：只标"已有睡袋"而不带温标，校验就只能判未知。
                for key, value in (own.specs or {}).items():
                    item.specs.setdefault(key, value)
                item.reason = _append(item.reason, f"已有（{own.source}）")
            else:
                item.qty -= own.qty
                # 数量不足时规格也要留下：扔掉规格等于把"已有的那件够不够用"
                # 这条信息一起丢了。剩下的缺口仍按待购处理，不拿旧规格冒充新商品。
                for key, value in (own.specs or {}).items():
                    item.specs.setdefault(key, value)
                item.reason = _append(
                    item.reason,
                    f"已有 {own.qty} 件（{own.source}），还差 {item.qty} 件",
                )
            break

    for index, own in enumerate(owned):
        if index in matched:
            continue
        gear.append(
            GearItem(
                name=own.name,
                qty=own.qty,
                reason=f"已有（{own.source}）",
                status="owned",
                specs=dict(own.specs or {}),
            )
        )
    return gear


def _append(reason: str, extra: str) -> str:
    if not extra:
        return reason
    if not reason:
        return extra
    if extra in reason:
        return reason
    return f"{reason}；{extra}"
