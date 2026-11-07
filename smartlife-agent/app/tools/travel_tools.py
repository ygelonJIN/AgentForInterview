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


# 示例目录，不是全量目的地库：`base_daily_budget` 是人均每日粗略估算（人民币），
# 只用于"这个预算够不够去"的粗筛，不当作可下单的价格。
# 城市名必须能在 `app/tools/geo.py` 的别名表里解析，否则天气和活动都拿不到。
_DESTINATIONS = [
    # 中国
    {"city": "杭州", "tags": ["文化", "休闲", "亲子", "美食"], "base_daily_budget": 600},
    {"city": "北京", "tags": ["文化", "历史", "亲子", "美食"], "base_daily_budget": 700},
    {"city": "上海", "tags": ["都市", "美食", "亲子", "购物"], "base_daily_budget": 800},
    {"city": "成都", "tags": ["美食", "休闲", "文化", "亲子"], "base_daily_budget": 550},
    {"city": "三亚", "tags": ["海滨", "亲子", "休闲", "户外"], "base_daily_budget": 900},
    {"city": "西安", "tags": ["历史", "文化", "美食", "亲子"], "base_daily_budget": 550},
    {"city": "丽江", "tags": ["户外", "休闲", "文化", "情侣"], "base_daily_budget": 650},
    {"city": "大理", "tags": ["户外", "休闲", "文化", "情侣"], "base_daily_budget": 650},
    # 亚洲
    {"city": "新加坡", "tags": ["都市", "亲子", "美食", "购物"], "base_daily_budget": 1100},
    {"city": "东京", "tags": ["都市", "美食", "购物", "文化"], "base_daily_budget": 1200},
    {"city": "大阪", "tags": ["美食", "购物", "亲子", "都市"], "base_daily_budget": 1050},
    {"city": "京都", "tags": ["文化", "历史", "美食", "情侣"], "base_daily_budget": 1100},
    {"city": "首尔", "tags": ["都市", "购物", "美食", "文化"], "base_daily_budget": 950},
    {"city": "曼谷", "tags": ["美食", "购物", "都市", "休闲"], "base_daily_budget": 700},
    {"city": "清迈", "tags": ["休闲", "文化", "美食", "户外"], "base_daily_budget": 600},
    {"city": "巴厘岛", "tags": ["海滨", "休闲", "情侣", "户外"], "base_daily_budget": 900},
    {"city": "吉隆坡", "tags": ["都市", "美食", "购物", "亲子"], "base_daily_budget": 650},
    {"city": "迪拜", "tags": ["都市", "购物", "亲子", "户外"], "base_daily_budget": 1600},
    {"city": "伊斯坦布尔", "tags": ["历史", "文化", "美食", "都市"], "base_daily_budget": 800},
    # 欧洲
    {"city": "巴黎", "tags": ["文化", "购物", "情侣", "美食"], "base_daily_budget": 1500},
    {"city": "伦敦", "tags": ["文化", "历史", "都市", "购物"], "base_daily_budget": 1600},
    {"city": "罗马", "tags": ["历史", "文化", "美食", "情侣"], "base_daily_budget": 1300},
    {"city": "巴塞罗那", "tags": ["文化", "美食", "海滨", "都市"], "base_daily_budget": 1200},
    {"city": "阿姆斯特丹", "tags": ["文化", "都市", "休闲", "购物"], "base_daily_budget": 1400},
    {"city": "圣托里尼", "tags": ["海滨", "情侣", "休闲", "户外"], "base_daily_budget": 1500},
    {"city": "雷克雅未克", "tags": ["户外", "自然", "情侣", "休闲"], "base_daily_budget": 1800},
    # 北美与大洋洲
    {"city": "纽约", "tags": ["都市", "购物", "文化", "美食"], "base_daily_budget": 1800},
    {"city": "洛杉矶", "tags": ["都市", "亲子", "购物", "户外"], "base_daily_budget": 1400},
    {"city": "温哥华", "tags": ["户外", "自然", "亲子", "休闲"], "base_daily_budget": 1300},
    {"city": "悉尼", "tags": ["海滨", "都市", "户外", "亲子"], "base_daily_budget": 1400},
    {"city": "奥克兰", "tags": ["户外", "自然", "休闲", "亲子"], "base_daily_budget": 1200},
    # 非洲
    {"city": "开普敦", "tags": ["自然", "户外", "海滨", "文化"], "base_daily_budget": 900},
    {"city": "马拉喀什", "tags": ["文化", "历史", "美食", "休闲"], "base_daily_budget": 700},
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
        """只读该城市自己的资料文件；没有就返回空。

        旧实现会兜底到 ``outdoor_activities.txt``，于是"新加坡"拿到了千岛湖露营和黄山
        —— 兜底不判断覆盖范围，等于把别的城市的资料冒充成本地证据，整块删掉。
        """
        raw = str(city).strip()
        names = [raw, raw.lower()]
        # 本地资料按拼音命名（hangzhou_activities.txt），中文城市名要经别名表转一次
        try:
            from app.tools import geo

            alias = geo.lookup_alias(raw)
            if alias:
                names.extend([
                    alias[1].lower().replace(" ", "_"),
                    alias[1].lower().replace(" ", ""),
                ])
        except Exception:
            pass
        for name in dict.fromkeys(item for item in names if item):
            path = self.activities_dir / f"{name}_activities.txt"
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _activity_items(text: str) -> List[str]:
        """条目名由统一解析器给，避免和 UI 目录页两套正则各说各话。"""
        from app.activity_notes import activity_titles

        return activity_titles(text)

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
                        "note": (
                            "示例目的地目录，日预算为人均粗略估算，不是可下单价格；"
                            "目录外的城市请直接说出目的地"
                        ),
                    })
            # 先看命中几个偏好，再看总花费；只命中一个标签的便宜城市不该压过多标签的
            results.sort(key=lambda row: (-len(row["matched_tags"]), row["estimated_total"]))
            return results

        @tool("get_local_activities", args_schema=GetLocalActivitiesInput)
        def get_local_activities(
            city: str,
            date: str,
            interests: Optional[List[str]] = None,
        ) -> List[Dict[str, Any]]:
            """查询指定城市当天的可去之处与真实活动，全球任意城市。

            数据来自 OpenStreetMap 的场所清单：区分"可买票参加的场所"与"只是可去的地方"，
            但**没有场次、档期与票价**，也不编时间。用户要参加具体活动时，
            由用户在需求里指定，系统负责排进日程并校验时间。
            只有该城市在本地资料目录里时才退回本地文件；都拿不到就返回
            ``available: False`` 并说明原因，不拿别的城市凑数。
            """
            from app.tools import geo
            from app.tools.providers import get_activities_provider

            warnings: List[str] = []
            items: List[Dict[str, Any]] = []
            source = ""
            kind = ""
            note = ""
            uncertain_city = False

            try:
                location = geo.resolve_location(city)
                english = geo.resolve_city_english(city)
                uncertain_city = location.uncertain
                provider = get_activities_provider()
                response = provider.fetch_with_meta({
                    "city": city,
                    "english": english,
                    "date": date,
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "country_code": location.country_code,
                })
                payload = response.payload
                items = list(payload.get("items") or [])
                source = str(payload.get("source") or provider.name)
                kind = str(payload.get("kind") or "")
                note = str(payload.get("note") or "")
                if response.stale:
                    warnings.append(f"活动数据来自过期缓存（{response.cache_status}）")
                warnings.extend(payload.get("warnings") or [])
            except Exception as exc:
                warnings.append(f"活动 provider 不可用: {type(exc).__name__}: {str(exc)[:160]}")

            if not items:
                local_text = read_activities(city)
                if local_text:
                    items = [
                        {"title": line, "kind": "本地资料", "city": city}
                        for line in activity_items(local_text)
                    ]
                    source = "local_activities_file"
                    kind = "local_file"
                    note = "该城市使用本地活动资料，不是在线数据"

            if interests and items:
                keywords = [str(tag).strip().lower() for tag in interests if str(tag).strip()]
                if keywords:
                    matched = [
                        item for item in items
                        if any(
                            keyword in f"{item.get('title', '')}{item.get('kind', '')}".lower()
                            for keyword in keywords
                        )
                    ]
                    items = matched or items

            if uncertain_city:
                warnings.append(f"{city} 不在已知城市表中，按最接近候选定位，结果可能不准")

            return [{
                "city": city,
                "date": date,
                "activities": items,
                "source": source or "unavailable",
                "kind": kind,
                "available": bool(items),
                "note": note,
                "warnings": list(dict.fromkeys(warnings)),
            }]

        @tool("plan_itinerary", args_schema=PlanItineraryInput)
        def plan_itinerary(
            destination: str,
            days: int,
            budget: float,
            interests: List[str],
            travel_style: Optional[str] = "休闲",
        ) -> Dict[str, Any]:
            """按目的地生成初始行程草案：本地资料优先，否则用在线真实场所。"""
            items = activity_items(read_activities(destination))
            plan_source = "local_activity_planner"
            if not items:
                # 本地没有该城市资料时不是"市区自由活动"了事，去拿真实场所
                try:
                    from app.tools import geo
                    from app.tools.providers import get_activities_provider

                    location = geo.resolve_location(destination)
                    payload = get_activities_provider().fetch_with_meta({
                        "city": destination,
                        "english": geo.resolve_city_english(destination),
                        "latitude": location.latitude,
                        "longitude": location.longitude,
                        "country_code": location.country_code,
                    }).payload
                    items = [
                        str(item.get("title") or "")
                        for item in (payload.get("items") or [])
                        if item.get("title")
                    ]
                    if items:
                        plan_source = f"{payload.get('source', 'online')}_planner"
                except Exception:
                    items = []
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
                "source": plan_source,
            }

        self.tools = [
            search_destinations,
            get_local_activities,
            plan_itinerary,
        ]
