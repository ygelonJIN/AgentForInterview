"""
Travel MCP Server - 旅游服务
"""

from typing import List, Dict, Any, Optional
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from .base import BaseMCPServer

class SearchDestinationsInput(BaseModel):
    """搜索目的地参数"""
    preference: str = Field(description="偏好描述，如：适合情侣、亲子、户外")
    budget: float = Field(description="预算（元）")
    days: int = Field(description="旅行天数")
    season: Optional[str] = Field(default=None, description="季节，如：春天、夏天")

class GetLocalActivitiesInput(BaseModel):
    """获取本地活动参数"""
    city: str = Field(description="城市名称")
    date: str = Field(description="日期，格式：YYYY-MM-DD")
    interests: Optional[List[str]] = Field(default=None, description="兴趣标签，如：美食、文化、运动")

class PlanItineraryInput(BaseModel):
    """规划行程参数"""
    destination: str = Field(description="目的地")
    days: int = Field(description="旅行天数")
    budget: float = Field(description="预算（元）")
    interests: List[str] = Field(description="兴趣标签")
    travel_style: Optional[str] = Field(default="休闲", description="旅行风格：休闲、冒险、文化")

class SearchHotelsInput(BaseModel):
    """搜索酒店参数"""
    city: str = Field(description="城市名称")
    check_in: str = Field(description="入住日期，格式：YYYY-MM-DD")
    check_out: str = Field(description="退房日期，格式：YYYY-MM-DD")
    budget_per_night: Optional[float] = Field(default=None, description="每晚预算")
    hotel_type: Optional[str] = Field(default=None, description="酒店类型：经济、舒适、豪华")

class TravelMCPServer(BaseMCPServer):
    """
    旅游MCP Server
    
    提供目的地推荐、行程规划、活动查询等服务
    """
    
    def __init__(self):
        super().__init__(
            name="travel-agent",
            description="旅游服务：目的地推荐、行程规划、活动查询、酒店预订"
        )
    
    def _initialize_tools(self):
        """初始化旅游工具"""
        
        @tool("search_destinations", args_schema=SearchDestinationsInput)
        def search_destinations(
            preference: str,
            budget: float,
            days: int,
            season: Optional[str] = None
        ) -> List[Dict[str, Any]]:
            """
            搜索目的地，根据偏好和预算推荐
            
            Args:
                preference: 偏好描述
                budget: 预算
                days: 旅行天数
                season: 季节
                
            Returns:
                List[Dict]: 目的地列表
            """
            # TODO: 实现目的地推荐
            return [
                {
                    "id": "dest_001",
                    "name": "杭州西湖",
                    "description": "适合情侣的浪漫目的地",
                    "budget_estimate": 2000,
                    "recommended_days": 2,
                    "best_season": "春天",
                    "rating": 4.8
                },
                {
                    "id": "dest_002",
                    "name": "厦门鼓浪屿",
                    "description": "文艺清新的海岛",
                    "budget_estimate": 2500,
                    "recommended_days": 3,
                    "best_season": "秋天",
                    "rating": 4.6
                }
            ]
        
        @tool("get_local_activities", args_schema=GetLocalActivitiesInput)
        def get_local_activities(
            city: str,
            date: str,
            interests: Optional[List[str]] = None
        ) -> List[Dict[str, Any]]:
            """
            获取本地活动信息
            
            Args:
                city: 城市名称
                date: 日期
                interests: 兴趣标签
                
            Returns:
                List[Dict]: 活动列表
            """
            # TODO: 实现活动查询
            return [
                {
                    "id": "activity_001",
                    "name": "西湖音乐喷泉",
                    "city": city,
                    "date": date,
                    "time": "19:00-20:00",
                    "location": "西湖湖滨",
                    "price": 0,
                    "description": "免费观赏的音乐喷泉表演"
                }
            ]
        
        @tool("plan_itinerary", args_schema=PlanItineraryInput)
        def plan_itinerary(
            destination: str,
            days: int,
            budget: float,
            interests: List[str],
            travel_style: str = "休闲"
        ) -> Dict[str, Any]:
            """
            规划行程
            
            Args:
                destination: 目的地
                days: 旅行天数
                budget: 预算
                interests: 兴趣标签
                travel_style: 旅行风格
                
            Returns:
                Dict: 行程安排
            """
            # TODO: 实现行程规划
            return {
                "destination": destination,
                "days": days,
                "budget": budget,
                "itinerary": [
                    {
                        "day": 1,
                        "activities": [
                            {"time": "09:00", "activity": "抵达目的地", "location": "机场/车站"},
                            {"time": "12:00", "activity": "午餐", "location": "当地特色餐厅"},
                            {"time": "14:00", "activity": "景点游览", "location": "主要景点"},
                            {"time": "18:00", "activity": "晚餐", "location": "美食街"}
                        ]
                    }
                ],
                "total_cost": budget * 0.8,
                "tips": ["建议提前预订酒店", "注意天气变化"]
            }
        
        @tool("search_hotels", args_schema=SearchHotelsInput)
        def search_hotels(
            city: str,
            check_in: str,
            check_out: str,
            budget_per_night: Optional[float] = None,
            hotel_type: Optional[str] = None
        ) -> List[Dict[str, Any]]:
            """
            搜索酒店
            
            Args:
                city: 城市
                check_in: 入住日期
                check_out: 退房日期
                budget_per_night: 每晚预算
                hotel_type: 酒店类型
                
            Returns:
                List[Dict]: 酒店列表
            """
            # TODO: 实现酒店搜索
            return [
                {
                    "id": "hotel_001",
                    "name": "西湖国宾馆",
                    "city": city,
                    "price_per_night": 800,
                    "rating": 4.9,
                    "type": "豪华",
                    "amenities": ["免费WiFi", "早餐", "游泳池"]
                }
            ]
        
        # 添加工具到列表
        self.tools = [
            search_destinations,
            get_local_activities,
            plan_itinerary,
            search_hotels
        ]
