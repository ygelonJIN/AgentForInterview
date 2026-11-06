"""缺必填槽位时，用一次小模型调用再读一遍原话。

规则的边界很清楚：目的地只有"去 X / 到 X"或地名表能认，天数只有"N 天"能认。
遇到"去那个有竹子的地方待几天"这种说法，规则一定抓不到，只能反问用户。

这里提供的是**可选的最后一道兜底**，不是替代规则：

- **默认关闭**（``SMARTLIFE_SLOT_ASSIST=1`` 才启用）。开启会多一次模型调用
  （约 0.5~1s），并且有"猜错"的风险——猜错比反问更糟。
- **只填空缺**：规则已经抽到的值一律不覆盖。
- **结果标注来源"模型补全"**：出现在约束清单里，用户能看出这是推断的。
- **防幻觉**：目的地必须能在原话里找到，或在已知地名表里；天数/人数/预算
  超出合理区间直接丢弃。
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.llm_json import ainvoke_json
from app.observability import log_exception

SWITCH_ENV = "SMARTLIFE_SLOT_ASSIST"
TIMEOUT_SECONDS = 4.0

_FIELD_LABELS = {
    "destination": "目的地",
    "days": "天数",
    "people": "人数",
    "budget": "预算",
    "start_date": "出发日期",
}


class _AssistedSlots(BaseModel):
    """模型只允许补这几个槽位；不确定就留空。"""

    destination: str = Field(default="", description="目的地地名；不确定写空串")
    days: Optional[int] = Field(default=None, description="出行天数；不确定写 null")
    people: Optional[int] = Field(default=None, description="同行人数（含本人）；不确定写 null")
    budget: Optional[float] = Field(default=None, description="总预算（元）；不确定写 null")
    start_date: str = Field(default="", description="出发日期，YYYY-MM-DD；不确定写空串")


_SYSTEM = """你是槽位抽取器。用户说了一句出行需求，规则引擎漏掉了部分信息，请你补出来。

硬规则：
1. 只抽取原话里**明确出现**的信息，绝不推断、绝不编造；
2. 不确定就留空（字符串写 ""，数字写 null）；
3. 目的地写地名本身（如"莫干山"），不要带"去/玩/露营"这类动词；
4. 人数包含说话人自己；"两个人""我俩""一家三口"都算；
5. 日期只认能确定到具体某天的表达，写成 YYYY-MM-DD；
6. 只输出 JSON 对象，不要解释。"""


def is_enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "").strip().lower() in {"1", "true", "on", "yes"}


def _clean_destination(value: str, message: str) -> str:
    text = str(value or "").strip()
    if not (2 <= len(text) <= 20):
        return ""
    # 防幻觉：地名必须能在原话里找到。模型无权把"那边"解释成具体城市：
    # 猜错一个目的地，整份行程都错，比反问用户代价大。
    if text in message:
        return text
    return ""


def _clean_days(value: Optional[int]) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 60 else None


def _clean_people(value: Optional[int]) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 100 else None


def _clean_budget(value: Optional[float]) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 10_000_000 else None


def _clean_date(value: str, today: Optional[str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        return ""
    if today:
        try:
            if parsed < date.fromisoformat(str(today)[:10]):
                return ""
        except ValueError:
            pass
    return parsed.isoformat()


def sanitize(raw: Dict[str, Any], message: str, missing: Iterable[str], today=None) -> Dict[str, Any]:
    """把模型输出收敛到"允许补的字段 + 合理范围"，越界的直接丢掉。"""
    allowed = set(missing)
    cleaned: Dict[str, Any] = {}
    if "destination" in allowed:
        value = _clean_destination(raw.get("destination", ""), message)
        if value:
            cleaned["destination"] = value
    if "days" in allowed:
        value = _clean_days(raw.get("days"))
        if value is not None:
            cleaned["days"] = value
    if "people" in allowed:
        value = _clean_people(raw.get("people"))
        if value is not None:
            cleaned["people"] = value
    if "budget" in allowed:
        value = _clean_budget(raw.get("budget"))
        if value is not None:
            cleaned["budget"] = value
    if "start_date" in allowed:
        value = _clean_date(raw.get("start_date", ""), today)
        if value:
            cleaned["start_date"] = value
    return cleaned


async def assist_slots(
    message: str,
    missing: Iterable[str],
    today=None,
    llm=None,
) -> Dict[str, Any]:
    """补全缺失槽位；开关关闭或模型失败时返回空字典。"""
    wanted: List[str] = [name for name in missing if name in _FIELD_LABELS]
    if not wanted or not str(message or "").strip() or not is_enabled():
        return {}

    if llm is None:
        from app.config import create_small_llm

        llm = create_small_llm(max_tokens=200)

    fields = "、".join(_FIELD_LABELS[name] for name in wanted)
    messages = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=f"需要补的字段：{fields}\n用户原话：{message}"),
    ]
    try:
        result = await ainvoke_json(llm, messages, _AssistedSlots, None)
    except Exception as exc:
        log_exception("nlu.slot_assist", exc, {"message": message})
        return {}

    return sanitize(result.model_dump(), message, wanted, today=today)
