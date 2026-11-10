"""跨场景任务的共享约束与结构化子任务模型。

槽位解析统一委托 ``app.nlu.slots``：这里是**同一份**解析结果的使用方，
不再自己维护一套正则，避免同一句话在共享约束和行程事实里抽出两个值。
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field

from app.nlu.slots import extract_slots
from app.nlu.slots import parse_budget as parse_budget_slot
from app.nlu.slots import parse_days as parse_days_slot


class SharedConstraints(BaseModel):
    """一次对话里跨场景共享的硬约束。``None`` 表示用户没说。"""

    budget: Optional[float] = Field(default=None, ge=0)
    people: Optional[int] = Field(default=None, ge=1, le=100)
    days: Optional[int] = Field(default=None, ge=1, le=60)
    start_date: Optional[date] = None
    destination: Optional[str] = None
    requirements: List[str] = Field(default_factory=list)

    @property
    def effective_people(self) -> int:
        """计算用的人数：用户没说就按 1 人算，但不假装是用户说的。"""
        return self.people or 1

    def prompt_context(self) -> str:
        values = {
            "预算": f"{self.budget:g} 元" if self.budget is not None else "未指定",
            "人数": (
                f"{self.people} 人"
                if self.people is not None
                else "未指定（按 1 人计）"
            ),
            "天数": f"{self.days} 天" if self.days is not None else "未指定",
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


def parse_budget(text: str) -> Optional[float]:
    """兼容旧入口：解析文本里的总预算。"""
    return parse_budget_slot(text)


def parse_days(text: str) -> Optional[int]:
    """兼容旧入口：解析文本里的天数。"""
    return parse_days_slot(text)


def _extra_requirements(slots) -> List[str]:
    """槽位之外的补充要求，直接给到 prompt 和复合综合。"""
    notes: List[str] = []
    if slots.with_kids:
        notes.append("同行有儿童")
    if slots.lodging:
        notes.append("住宿：露营" if slots.lodging == "camp" else "住宿：酒店/民宿")
    if slots.transport:
        notes.append(
            {"flight": "交通：飞机", "train": "交通：高铁/火车", "drive": "交通：自驾"}.get(
                slots.transport, f"交通：{slots.transport}"
            )
        )
    if slots.gear_budget is not None:
        notes.append(f"装备预算：{slots.gear_budget:g} 元")
    return notes


def extract_shared_constraints(message: str, today=None) -> SharedConstraints:
    """无模型快速抽取跨场景共享硬约束；与行程事实共用同一份槽位。"""
    slots = extract_slots(message, today=today)
    days = slots.days
    notes = _extra_requirements(slots)
    if days is not None and days > 60:
        notes.append(f"原话是 {days} 天，超出单次规划上限，按 60 天处理")
        days = 60
    return SharedConstraints(
        budget=slots.budget,
        people=slots.people,
        days=days,
        start_date=date.fromisoformat(slots.start_date) if slots.start_date else None,
        destination=slots.destination or None,
        requirements=notes,
    )


def build_composite_plan(message: str, today=None) -> CompositePlan:
    constraints = extract_shared_constraints(message, today=today)
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
