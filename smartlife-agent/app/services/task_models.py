"""跨场景任务的共享约束与结构化子任务模型。"""
from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field


class SharedConstraints(BaseModel):
    budget: Optional[float] = Field(default=None, ge=0)
    people: int = Field(default=1, ge=1, le=100)
    days: Optional[int] = Field(default=None, ge=1, le=60)
    start_date: Optional[date] = None
    destination: Optional[str] = None
    requirements: List[str] = Field(default_factory=list)

    def prompt_context(self) -> str:
        values = {
            "预算": f"{self.budget:g} 元" if self.budget is not None else "未指定",
            "人数": self.people,
            "天数": self.days,
            "开始日期": self.start_date.isoformat() if self.start_date else "未指定",
            "目的地": self.destination or "未指定",
            "补充要求": "、".join(self.requirements) or "无",
        }
        return "\n".join(f"- {key}: {value}" for key, value in values.items())


class SubTask(BaseModel):
    intent: str
    instruction: str
    constraints: SharedConstraints = Field(default_factory=SharedConstraints)


class CompositePlan(BaseModel):
    constraints: SharedConstraints = Field(default_factory=SharedConstraints)
    subtasks: List[SubTask] = Field(default_factory=list)
    evidence_sources: List[str] = Field(default_factory=list)


class BranchOutcome(BaseModel):
    name: str
    status: str
    response: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and not self.error


_DESTINATIONS = (
    "杭州", "北京", "上海", "成都", "三亚", "西安", "丽江", "大理",
    "厦门", "桂林", "张家界", "西藏", "新疆", "云南",
)


_CN_DIGITS = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "半": 0.5,
}

# 天数：阿拉伯数字、中文数字、"N 周 / 星期 / 礼拜 / 半个多月"
_DAY_RE = re.compile(r"(\d+)\s*(?:天|日)")
_CN_DAY_RE = re.compile(r"(十[一二三四五六七八九]?|[一二两三四五六七八九])\s*(?:天|日)")
_WEEK_RE = re.compile(r"(\d+|[一二两三四五六七八九十]|半)\s*(?:个)?\s*(?:周|星期|礼拜)")
_HALF_MONTH_RE = re.compile(r"(?:半|0\.5)\s*个?\s*月")

# 预算：1万 / 1.5万 / 1万5 / 一万五 / 3千 / 800元 / 预算3000
_BUDGET_WAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万\s*([一二三四五六七八九\d])?")
_BUDGET_CN_WAN_RE = re.compile(r"([一两二三四五六七八九十])\s*万\s*([一二三四五六七八九])?")
_BUDGET_QIAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*千")
_BUDGET_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|¥)")
_BUDGET_CONTEXT_RE = re.compile(r"(?:预算|不超过|以内|低于|最高)\D{0,8}(\d+(?:\.\d+)?)")


def _cn_number(token: str) -> float:
    """把单个中文数字/半转成数值；十X 也能处理。"""
    token = (token or "").strip()
    if not token:
        return 0.0
    if token in _CN_DIGITS:
        return float(_CN_DIGITS[token])
    if token.startswith("十"):
        rest = token[1:]
        return 10.0 + (_CN_DIGITS.get(rest, 0) if rest else 0)
    return 0.0


def parse_budget(text: str) -> Optional[float]:
    """从自然语言里取预算金额，支持 万 / 千 / 元 / 块 与中文数字。

    例：预算1万块 -> 10000；1.5万 -> 15000；一万五 -> 15000；3千 -> 3000。
    """
    source = text or ""
    match = _BUDGET_WAN_RE.search(source)
    if match:
        base = float(match.group(1)) * 10000
        tail = match.group(2)
        if tail:
            base += (_CN_DIGITS.get(tail, float(tail) if tail.isdigit() else 0) or 0) * 1000
        return base
    match = _BUDGET_CN_WAN_RE.search(source)
    if match:
        base = _cn_number(match.group(1)) * 10000
        tail = match.group(2)
        if tail:
            base += _cn_number(tail) * 1000
        return base
    match = _BUDGET_QIAN_RE.search(source)
    if match:
        return float(match.group(1)) * 1000
    match = _BUDGET_UNIT_RE.search(source)
    if match:
        return float(match.group(1))
    match = _BUDGET_CONTEXT_RE.search(source)
    if match:
        return float(match.group(1))
    return None


def parse_days(text: str) -> Optional[int]:
    """从自然语言里取天数，支持 7天 / 七天 / 一周 / 两周 / 半个月。"""
    source = text or ""
    match = _DAY_RE.search(source)
    if match:
        return int(match.group(1))
    match = _CN_DAY_RE.search(source)
    if match:
        return int(_cn_number(match.group(1)))
    match = _HALF_MONTH_RE.search(source)
    if match:
        return 15
    match = _WEEK_RE.search(source)
    if match:
        token = match.group(1)
        if token == "半":
            return 4
        weeks = float(token) if token.isdigit() else _cn_number(token)
        return max(1, int(round(weeks * 7)))
    return None


def extract_shared_constraints(message: str) -> SharedConstraints:
    """无模型快速抽取跨场景共享硬约束。"""
    text = message or ""
    people_match = re.search(r"(\d+)\s*人", text)
    date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    destination = next((item for item in _DESTINATIONS if item in text), None)
    return SharedConstraints(
        budget=parse_budget(text),
        people=int(people_match.group(1)) if people_match else 1,
        days=parse_days(text),
        start_date=date.fromisoformat(date_match.group(1)) if date_match else None,
        destination=destination,
    )


def build_composite_plan(message: str) -> CompositePlan:
    constraints = extract_shared_constraints(message)
    shopping_subtask = "完成用户明确提到的购物、商品比较或装备清单任务。"
    travel_subtask = "完成用户明确提到的目的地、行程、活动或出行计划任务。"
    return CompositePlan(
        constraints=constraints,
        subtasks=[
            SubTask(intent="shopping", instruction=shopping_subtask, constraints=constraints),
            SubTask(intent="travel", instruction=travel_subtask, constraints=constraints),
        ],
        evidence_sources=["sql", "rag", "local_guides", "weather", "route", "memory"],
    )


def branch_outcome(name: str, response: str = "", error: str = "") -> BranchOutcome:
    return BranchOutcome(
        name=name,
        status="ok" if not error else "failed",
        response=response or "",
        error=error or "",
    )
