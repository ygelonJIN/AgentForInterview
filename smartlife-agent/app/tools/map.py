"""
地图查询工具 - Function Calling
"""
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import random

class MapInput(BaseModel):
    origin: str = Field(description="起点")
    destination: str = Field(description="终点")
    mode: str = Field(default="driving", description="交通方式：driving/walking/transit")

@tool("get_route", args_schema=MapInput)
def get_route(origin: str, destination: str, mode: str = "driving") -> dict:
    """获取两点之间的路线和距离信息"""
    distance = random.uniform(1, 50)
    speed_map = {"driving": 40, "walking": 5, "transit": 25}
    speed = speed_map.get(mode, 30)
    duration = distance / speed * 60
    
    mode_names = {"driving": "驾车", "walking": "步行", "transit": "公共交通"}
    
    return {
        "origin": origin,
        "destination": destination,
        "mode": mode_names.get(mode, mode),
        "distance": f"{distance:.1f}公里",
        "duration": f"{duration:.0f}分钟",
        "route": f"从{origin}出发，经主要道路到达{destination}"
    }

def get_map_tools():
    return [get_route]
