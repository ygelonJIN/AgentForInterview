"""
天气查询工具 - Function Calling
"""
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import random
from datetime import datetime

class WeatherInput(BaseModel):
    city: str = Field(description="城市名称")
    date: str = Field(description="日期，格式 YYYY-MM-DD")

WEATHER_DATA = {
    "杭州": {"temp_range": (15, 28), "conditions": ["晴", "多云", "小雨"], "humidity": (60, 85)},
    "北京": {"temp_range": (5, 25), "conditions": ["晴", "多云", "雾霾"], "humidity": (20, 60)},
    "上海": {"temp_range": (18, 30), "conditions": ["多云", "小雨", "阴"], "humidity": (70, 90)},
    "成都": {"temp_range": (16, 26), "conditions": ["多云", "阴", "小雨"], "humidity": (65, 85)},
    "三亚": {"temp_range": (24, 33), "conditions": ["晴", "多云"], "humidity": (75, 95)},
    "西安": {"temp_range": (8, 24), "conditions": ["晴", "多云", "沙尘"], "humidity": (30, 60)},
    "丽江": {"temp_range": (10, 22), "conditions": ["晴", "多云"], "humidity": (40, 70)},
    "大理": {"temp_range": (12, 24), "conditions": ["晴", "多云", "小雨"], "humidity": (50, 75)},
}

@tool("get_weather", args_schema=WeatherInput)
def get_weather(city: str, date: str) -> dict:
    """获取指定城市和日期的天气信息，包括温度、天气状况、湿度"""
    if city in WEATHER_DATA:
        data = WEATHER_DATA[city]
        temp = random.randint(*data["temp_range"])
        condition = random.choice(data["conditions"])
        humidity = random.randint(*data["humidity"])
        return {
            "city": city,
            "date": date,
            "temperature": f"{temp}°C",
            "condition": condition,
            "humidity": f"{humidity}%",
            "suggestion": "适合出行" if condition in ["晴", "多云"] else "建议室内活动"
        }
    return {"city": city, "date": date, "temperature": "20°C", "condition": "晴", "humidity": "50%", "suggestion": "适合出行"}

def get_weather_tools():
    return [get_weather]
