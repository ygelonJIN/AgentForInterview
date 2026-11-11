"""预算分配：行程与装备各拿多少。

为什么要有这一层：trip_gear 里行程和装备都要，"各占多少"是用户的选择而不是
系统该猜的比例。以前只有 ``budget_scope``（含不含装备）和 ``gear_budget``
（用户主动说"装备预算 1000"），中间的默认比例没人定，结果装备段拿到
"总预算 − 行程花费"的全部余额——行程省下的钱全变成更贵的装备。

这里只做**纯解析**：把用户在追问卡片里选的档位（或自己填的数）翻成
``(trip_cap, gear_cap)``。不查库、不调模型。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# 档位标签 → 装备占比。三档都在“两边都要”的前提下表达倾斜，
# 不提供“只排行程”（那本来就该走 travel，而不是 trip_gear）。
GEAR_RATIOS = {
    "80/20": 0.2,
    "50/50": 0.5,
    "20/80": 0.8,
}
DEFAULT_LABEL = "80/20"
DEFAULT_SOURCE = "默认（未选择时按行程优先）"

_RATIO_RE = re.compile(r"(\d{1,3})\s*[/:：]\s*(\d{1,3})")
# 滑动条只回一个整数：直接当"行程占比"
_BARE_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3})(?!\d)")
_GEAR_RATIO_RE = re.compile(r"装备\s*(?:占|比例)?\s*(\d{1,3})\s*%")
_TRIP_AMOUNT_RE = re.compile(r"行程(?:预算)?\s*(?:[:：]|占)?\s*(\d+(?:\.\d+)?)\s*元?")
_GEAR_AMOUNT_RE = re.compile(r"装备(?:预算)?\s*(?:[:：]|占)?\s*(\d+(?:\.\d+)?)\s*元?")


def options() -> list[Dict[str, str]]:
    """追问卡片用的三档选项（加一个"自己填"由 UI 提供）。"""
    return [
        {"label": "行程优先（装备 20%）", "value": "80/20"},
        {"label": "各一半（装备 50%）", "value": "50/50"},
        {"label": "装备优先（装备 80%）", "value": "20/80"},
    ]


def caps(budget: float, gear_cap: float) -> Tuple[float, float]:
    """把装备额度夹在 [0, 预算] 内，剩下的就是行程额度。"""
    total = max(0.0, float(budget))
    gear = min(max(0.0, float(gear_cap)), total)
    return round(total - gear, 2), round(gear, 2)


def parse(text: str, budget: float) -> Optional[Dict[str, Any]]:
    """解析用户的答案；认不出来返回 None（调用方回落到默认档）。

    接受四种写法：
    - 滑动条回的裸数字 ``80``（= 行程占比 80%）
    - 档位 ``80/20``（行程/装备）
    - 装备占比 ``装备 30%``
    - 金额 ``装备预算 1000`` / ``行程 4000``
    """
    raw = str(text or "").strip()
    total = float(budget or 0)
    if not raw or total <= 0:
        return None

    match = _RATIO_RE.search(raw)
    if match:
        trip_part, gear_part = int(match.group(1)), int(match.group(2))
        if trip_part + gear_part > 0:
            gear = total * gear_part / (trip_part + gear_part)
            return {"gear_cap": round(gear, 2), "trip_pct": trip_part,
                    "label": f"{trip_part}/{gear_part}", "source": "用户选择"}

    match = _GEAR_RATIO_RE.search(raw)
    if match:
        pct = max(0, min(100, int(match.group(1))))
        return {"gear_cap": round(total * pct / 100, 2), "trip_pct": 100 - pct,
                "label": "自定义", "source": "用户选择"}

    match = _GEAR_AMOUNT_RE.search(raw)
    if match:
        gear = max(0.0, min(float(match.group(1)), total))
        return {"gear_cap": round(gear, 2), "trip_pct": round((1 - gear / total) * 100),
                "label": "自定义", "source": "用户选择"}

    match = _TRIP_AMOUNT_RE.search(raw)
    if match:
        trip = max(0.0, min(float(match.group(1)), total))
        return {"gear_cap": round(total - trip, 2), "trip_pct": round(trip / total * 100),
                "label": "自定义", "source": "用户选择"}

    # 滑动条：一个 0~100 的整数 = 行程占比（放在金额/百分比之后，
    # 否则"装备 30%"会被当成"行程 30%"）
    match = _BARE_PERCENT_RE.search(raw)
    if match:
        trip_pct = max(0, min(100, int(match.group(1))))
        return {"gear_cap": round(total * (100 - trip_pct) / 100, 2),
                "trip_pct": trip_pct, "label": f"{trip_pct}/{100 - trip_pct}",
                "source": "用户选择"}

    return None
