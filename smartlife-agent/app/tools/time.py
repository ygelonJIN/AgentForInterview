"""
时间查询工具 - Function Calling
"""
from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
import pytz

class TimeInput(BaseModel):
    timezone: str = Field(default="Asia/Shanghai", description="时区")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            pytz.timezone(value)
        except pytz.UnknownTimeZoneError as exc:
            raise ValueError(f"未知时区: {value}") from exc
        return value

@tool("get_current_time", args_schema=TimeInput)
def get_current_time(timezone: str = "Asia/Shanghai") -> dict:
    """获取当前时间"""
    tz = pytz.timezone(timezone)
    
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
