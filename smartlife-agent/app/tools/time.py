"""
时间查询工具 - Function Calling
"""
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import pytz

class TimeInput(BaseModel):
    timezone: str = Field(default="Asia/Shanghai", description="时区")

@tool("get_current_time", args_schema=TimeInput)
def get_current_time(timezone: str = "Asia/Shanghai") -> dict:
    """获取当前时间"""
    try:
        tz = pytz.timezone(timezone)
    except:
        tz = pytz.timezone("Asia/Shanghai")
    
    now = datetime.now(tz)
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    
    return {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": weekdays[now.weekday()],
        "timezone": str(tz),
        "is_weekend": now.weekday() >= 5
    }

def get_time_tools():
    return [get_current_time]
