"""让模型从候选里挑款。

为什么不是"每件装备问一次"：一次行程十来项装备，那样就是十来次调用，
成本和延迟都不可接受。这里**一次调用挑完所有装备**，而且：

- 候选已经被打分器裁到每项 4 个（`select.rank`），模型只做"四选一"，不做检索；
- 只给 id / 名称 / 价格 / 一两个关键参数，不给完整描述；
- 同一组输入命中进程内缓存（一小时），重复行程不再付费；
- 模型没选、选错 id、超预算、超时 → 一律回落到打分结果，链路不阻塞。

所以成本大概是"每次出行一次调用、约 1k token"，和逐件调用差一个数量级。
开关：``SMARTLIFE_MODEL_SELECT=0`` 可关闭。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.llm_json import ainvoke_json
from app.observability import log_exception

SWITCH_ENV = "SMARTLIFE_MODEL_SELECT"
TIMEOUT_SECONDS = 8.0
MAX_ITEMS = 12
MAX_CANDIDATES = 4
CACHE_TTL_SECONDS = 3600.0
CACHE_MAX_ENTRIES = 128

_cache: "OrderedDict[str, tuple]" = OrderedDict()
_lock = threading.RLock()


class _Choice(BaseModel):
    gear: str = Field(description="装备名，原样抄回题目里的名字")
    product_id: Optional[int] = Field(default=None, description="候选里的商品 id；选不出写 null")
    reason: str = Field(default="", description="一句话理由")


class _Choices(BaseModel):
    choices: List[_Choice] = Field(default_factory=list)


_SYSTEM = """你是选品助手。给你这次出行的若干装备需求，以及每项需求下的候选商品，
请为每项挑一件最合适的。

规则：
1. 只能从该项的候选里选，product_id 必须是候选里出现过的；选不出写 null；
2. 优先满足场景与天气（温标、防水、容量这类参数要与需求匹配）；
3. 不能超出剩余装备预算；同样的钱优先选参数与口碑更好的款；
4. 同一个商品只能给一项装备用；
5. 用户点名"一套衣服/穿搭"时，优先能单独成一套的连衣裙；半裙、上衣只在
   预算不够时选，并在理由里写明缺的是上衣还是下装；
6. 只输出 JSON，不要解释。结构：
{"choices": [{"gear": "睡袋", "product_id": 123, "reason": "温标 -5℃ 满足夜间 3℃"}]}"""


def is_enabled() -> bool:
    value = os.environ.get(SWITCH_ENV, "").strip().lower()
    if value in {"0", "false", "off", "no"}:
        return False
    return True


def _spec_summary(product: Dict[str, Any], limit: int = 3) -> str:
    raw = product.get("specs")
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return ""
    if not isinstance(raw, dict):
        return ""
    parts = []
    for key, value in list(raw.items())[:limit]:
        if value in (None, "", []):
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)


def build_entries(
    items: Sequence[Any],
    candidates_for,
    rank,
    *,
    facts: Any = None,
    remaining: Optional[float] = None,
    preferred_brands: Optional[Sequence[str]] = None,
    max_items: int = MAX_ITEMS,
    max_candidates: int = MAX_CANDIDATES,
) -> List[Dict[str, Any]]:
    """给每项装备准备候选；候选少于 2 个时不问模型（没有可挑的）。"""
    entries: List[Dict[str, Any]] = []
    for item in list(items)[:max_items]:
        if item.status != "need_buy":
            continue
        rows = candidates_for(item)
        if len(rows) < 2:
            continue
        ranked = rank(
            rows,
            item.name,
            item.name,
            facts=facts,
            remaining=remaining,
            qty=item.qty,
            preferred_brands=preferred_brands,
        )
        chosen = [entry.product for entry in ranked[:max_candidates]]
        if len(chosen) < 2:
            continue
        entries.append({
            "gear": item.name,
            "qty": item.qty,
            "reason": item.reason,
            "candidates": chosen,
        })
    return entries


def _cache_key(entries: List[Dict[str, Any]], remaining: Optional[float]) -> str:
    payload = {
        "entries": [
            {
                "gear": entry["gear"],
                "qty": entry["qty"],
                "ids": [candidate.get("id") for candidate in entry["candidates"]],
            }
            for entry in entries
        ],
        "remaining": None if remaining is None else round(float(remaining), -2),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _cache_get(key: str) -> Optional[Dict[str, Dict[str, Any]]]:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if not hit:
            return None
        created_at, value = hit
        if now - created_at > CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return dict(value)


def _cache_put(key: str, value: Dict[str, Dict[str, Any]]) -> None:
    with _lock:
        _cache[key] = (time.monotonic(), value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _render(entries: List[Dict[str, Any]], facts: Any, remaining: Optional[float]) -> str:
    lines = []
    if facts is not None:
        lines.append(
            f"行程：{getattr(facts, 'destination', '') or '未指定'} "
            f"{getattr(facts, 'days', '') or '?'} 天，"
            f"人数 {getattr(facts, 'effective_people', 1)}，"
            f"天气 {getattr(facts, 'weather_summary', '') or '未知'}"
        )
    lines.append(f"剩余装备预算：{'未限定' if remaining is None else f'{remaining:.0f} 元'}")
    for index, entry in enumerate(entries, start=1):
        lines.append(f"{index}) {entry['gear']} ×{entry['qty']}（{entry['reason']}）")
        for candidate in entry["candidates"]:
            specs = _spec_summary(candidate)
            lines.append(
                f"   - id={candidate.get('id')} {candidate.get('name')} "
                f"¥{float(candidate.get('price') or 0):g}"
                f"{' ' + specs if specs else ''}"
            )
    return "\n".join(lines)


def _validate(
    choices: Iterable[Any],
    entries: List[Dict[str, Any]],
    remaining: Optional[float],
) -> Dict[str, Dict[str, Any]]:
    """只接受"在候选里、不超预算、不重复"的选择。"""
    by_gear = {entry["gear"]: entry for entry in entries}
    used = set()
    accepted: Dict[str, Dict[str, Any]] = {}
    budget = remaining

    for choice in choices or []:
        gear = str(getattr(choice, "gear", "") or "").strip()
        entry = by_gear.get(gear)
        if not entry:
            continue
        product_id = getattr(choice, "product_id", None)
        if product_id is None:
            continue
        matched = next(
            (candidate for candidate in entry["candidates"] if candidate.get("id") == product_id),
            None,
        )
        if matched is None or product_id in used:
            continue
        cost = float(matched.get("price") or 0) * int(entry["qty"])
        if budget is not None and cost > budget:
            continue
        used.add(product_id)
        if budget is not None:
            budget -= cost
        accepted[gear] = dict(matched)
    return accepted


async def choose_products(
    entries: List[Dict[str, Any]],
    facts: Any = None,
    remaining: Optional[float] = None,
    llm=None,
    timeout: float = TIMEOUT_SECONDS,
) -> Dict[str, Dict[str, Any]]:
    """返回 {装备名: 商品行}；任何异常都返回空字典，由打分选品兜底。"""
    if not entries or not is_enabled():
        return {}

    key = _cache_key(entries, remaining)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    if llm is None:
        from app.config import create_small_llm

        llm = create_small_llm(max_tokens=600)

    messages = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=_render(entries, facts, remaining)),
    ]
    try:
        result = await asyncio.wait_for(
            ainvoke_json(llm, messages, _Choices, None), timeout=timeout
        )
    except Exception as exc:
        log_exception("trip_gear.model_select", exc)
        return {}

    accepted = _validate(result.choices, entries, remaining)
    if accepted:
        _cache_put(key, accepted)
    return accepted
