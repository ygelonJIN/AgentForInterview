"""跨场景任务的共享约束与结构化子任务模型。"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

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


class CompositeOutcome(BaseModel):
    shopping: BranchOutcome
    travel: BranchOutcome
    merged_response: str
    shared_constraints: SharedConstraints = Field(default_factory=SharedConstraints)
    synthesis_status: str = "fallback"


_DESTINATIONS = (
    "杭州", "北京", "上海", "成都", "三亚", "西安", "丽江", "大理",
    "厦门", "桂林", "张家界", "西藏", "新疆", "云南",
)


def extract_shared_constraints(message: str) -> SharedConstraints:
    """无模型快速抽取跨场景共享硬约束。"""
    text = message or ""
    budget_match = re.search(r"(预算|不超过|以内|低于|最高)\D{0,8}(\d+(?:\.\d+)?)\s*(?:元|块|¥)?", text)
    if not budget_match:
        budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|¥)\s*(?:预算|以内|以下|以下预算)?", text)
    day_match = re.search(r"(\d+)\s*(?:天|日)", text)
    chinese_days = {
        "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    chinese_day_match = re.search(r"([一两二三四五六七八九十])\s*(?:天|日)", text)
    people_match = re.search(r"(\d+)\s*人", text)
    date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    destination = next((item for item in _DESTINATIONS if item in text), None)
    budget = float(budget_match.group(2) if budget_match and budget_match.lastindex and budget_match.lastindex >= 2 else budget_match.group(1)) if budget_match else None
    return SharedConstraints(
        budget=budget,
        people=int(people_match.group(1)) if people_match else 1,
        days=(
            int(day_match.group(1)) if day_match
            else chinese_days.get(chinese_day_match.group(1)) if chinese_day_match
            else None
        ),
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
