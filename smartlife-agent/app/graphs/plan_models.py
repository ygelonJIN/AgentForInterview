"""通用任务规划的结构化模型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class GeneralPlanStep(BaseModel):
    title: str
    instruction: str
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    status: str = "pending"
    error: str = ""


class GeneralTaskPlan(BaseModel):
    summary: str = ""
    steps: List[GeneralPlanStep] = Field(default_factory=list)
    budget: Optional[float] = Field(default=None, ge=0)
    constraints: List[str] = Field(default_factory=list)
