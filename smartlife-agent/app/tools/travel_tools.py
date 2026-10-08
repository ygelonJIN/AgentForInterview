"""旅游工具契约，底层连接本地目的地和活动资料。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from app.tools.tool_bundle import ToolBundle


class SearchDestinationsInput(BaseModel):
    preference: str = Field(description="偏好描述，如：适合情侣、亲子、户外")
    budget: float = Field(gt=0, description="预算（元）")
    days: int = Field(gt=0, le=30, description="旅行天数")
    season: Optional[str] = Field(default=None, description="季节，如：春天、夏天")


class GetLocalActivitiesInput(BaseModel):
    city: str = Field(min_length=1, description="城市名称")
    date: str = Field(description="日期，格式：YYYY-MM-DD")
    interests: Optional[List[str]] = Field(default=None, description="兴趣标签，如：美食、文化、运动")

    @field_validator("date")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("date 必须使用 YYYY-MM-DD 格式")
        return value


class PlanItineraryInput(BaseModel):
    destination: str = Field(min_length=1, description="目的地")
    days: int = Field(gt=0, le=30, description="旅行天数")
    budget: float = Field(gt=0, description="预算（元）")
    interests: List[str] = Field(description="兴趣标签")
    travel_style: Optional[str] = Field(default="休闲", description="旅行风格：休闲、冒险、文化")


_DESTINATIONS = [
    {"city": "杭州", "tags": ["文化", "休闲", "亲子", "美食"], "base_daily_budget": 600},
    {"city": "北京", "tags": ["文化", "历史", "亲子", "美食"], "base_daily_budget": 700},
    {"city": "上海", "tags": ["都市", "美食", "亲子", "购物"], "base_daily_budget": 800},
    {"city": "成都", "tags": ["美食", "休闲", "文化", "亲子"], "base_daily_budget": 550},
    {"city": "三亚", "tags": ["海滨", "亲子", "休闲", "户外"], "base_daily_budget": 900},
    {"city": "西安", "tags": ["历史", "文化", "美食", "亲子"], "base_daily_budget": 550},
    {"city": "丽江", "tags": ["户外", "休闲", "文化", "情侣"], "base_daily_budget": 650},
    {"city": "大理", "tags": ["户外", "休闲", "文化", "情侣"], "base_daily_budget": 650},
]


class TravelToolProvider(ToolBundle):
    """提供真实目录数据的旅行辅助工具。"""

    def __init__(self, activities_dir: Optional[Path] = None):
        self.activities_dir = (
            Path(activities_dir)
            if activities_dir
            else Path(__file__).resolve().parents[2] / "data" / "activities"
        )
        super().__init__(
            name="travel-agent",
            description="旅游服务：目的地推荐、行程规划和本地活动查询",
        )

    def _read_activities(self, city: str) -> str:
        for path in (
            self.activities_dir / f"{city}_activities.txt",
            self.activities_dir / "outdoor_activities.txt",
        ):
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _activity_items(text: str) -> List[str]:
        return [
            line.strip()
            for line in text.splitlines()
            if re.match(r"^\d+\.\s+", line.strip())
        ]

    def _initialize_tools(self):
        destinations = tuple(_DESTINATIONS)
        read_activities = self._read_activities
        activity_items = self._activity_items

        @tool("search_destinations", args_schema=SearchDestinationsInput)
        def search_destinations(
            preference: str,
            budget: float,
            days: int,
            season: Optional[str] = None,
        ) -> List[Dict[str, Any]]:
            """按偏好、预算和天数筛选本地目的地目录。"""
            preference_text = f"{preference} {season or ''}".lower()
            results = []
            for item in destinations:
                matched_tags = [tag for tag in item["tags"] if tag.lower() in preference_text]
                estimated = item["base_daily_budget"] * days
                if estimated <= budget or matched_tags:
                    results.append({
                        **item,
                        "estimated_total": estimated,
                        "matched_tags": matched_tags,
                        "source": "local_destination_catalog",
                    })
            results.sort(key=lambda row: (not row["matched_tags"], row["estimated_total"]))
            return results

        @tool("get_local_activities", args_schema=GetLocalActivitiesInput)
        def get_local_activities(
            city: str,
            date: str,
            interests: Optional[List[str]] = None,
        ) -> List[Dict[str, Any]]:
            """读取当前城市的本地活动资料，并按兴趣过滤。"""
            text = read_activities(city)
            items = activity_items(text)
            if interests:
                normalized = [str(item).strip().lower() for item in interests if str(item).strip()]
                items = [item for item in items if any(tag in item.lower() for tag in normalized)]
            return [{
                "city": city,
                "date": date,
                "activities": items,
                "source": "local_activities_file",
                "available": bool(items),
            }]

        @tool("plan_itinerary", args_schema=PlanItineraryInput)
        def plan_itinerary(
            destination: str,
            days: int,
            budget: float,
            interests: List[str],
            travel_style: Optional[str] = "休闲",
        ) -> Dict[str, Any]:
            """基于本地活动资料生成可执行的初始行程草案。"""
            items = activity_items(read_activities(destination))
            selected = [
                item for item in items
                if not interests or any(tag in item.lower() for tag in interests)
            ] or items
            itinerary = []
            for index in range(days):
                candidates = selected[index * 2:index * 2 + 2]
                itinerary.append({
                    "day": index + 1,
                    "activities": candidates or [f"{destination}市区自由活动"],
                })
            return {
                "destination": destination,
                "days": days,
                "budget": budget,
                "travel_style": travel_style or "休闲",
                "itinerary": itinerary,
                "source": "local_activity_planner",
            }

        self.tools = [
            search_destinations,
            get_local_activities,
            plan_itinerary,
        ]
