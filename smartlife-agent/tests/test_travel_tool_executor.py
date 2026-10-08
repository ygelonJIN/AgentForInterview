"""旅游步骤真实工具执行测试。"""

import asyncio

from app.tools.travel_executor import TravelToolExecutor


class _Tool:
    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        return self.result


def test_travel_executor_calls_weather_and_route_tools():
    weather = _Tool("weather", {
        "temperature": "21°C", "condition": "晴", "simulated": False, "source": "weather"
    })
    route = _Tool("route", {
        "distance": "3公里", "duration": "10分钟", "simulated": False, "source": "route"
    })
    executor = TravelToolExecutor(
        weather_tool=weather,
        route_tool=route,
        activities_dir="/tmp/no-activities",
    )

    result = asyncio.run(executor.execute(
        {
            "id": "day-1",
            "title": "第1天：西湖到灵隐寺",
            "description": "从西湖到灵隐寺，晚上入住酒店",
        },
        {
            "requirements": {
                "destination": "杭州",
                "days": 2,
                "budget": 1000,
                "people": 2,
                "start_date": "2026-10-10",
            }
        },
    ))

    assert result["status"] == "completed"
    assert result["source"] == "travel_tool_registry"
    assert result["simulated"] is False
    assert [item["tool"] for item in result["tool_calls"]] == ["get_weather", "get_route"]
    assert weather.calls == [{"city": "杭州", "date": "2026-10-10"}]
    assert route.calls == [{
        "origin": "西湖",
        "destination": "灵隐寺",
        "mode": "driving",
        "city": "杭州",
    }]


def test_travel_executor_rejects_non_current_provider_results():
    common = {"stale": True, "source": "provider"}
    executor = TravelToolExecutor(
        weather_tool=_Tool("weather", {"temperature": "20°C", "condition": "晴", **common}),
        route_tool=_Tool("route", {"distance": "1公里", "duration": "5分钟", **common}),
        activities_dir="/tmp/no-activities",
    )

    result = asyncio.run(executor.execute(
        {"id": "day-1", "title": "第1天", "description": "入住酒店"},
        {"requirements": {"destination": "杭州", "days": 1, "start_date": "2026-10-10"}},
    ))

    assert result["simulated"] is False
    assert all(item["ok"] is False for item in result["tool_calls"])
    assert all(item["result"]["accepted"] is False for item in result["tool_calls"])
    assert "20°C" not in result["result"]
    assert "1公里" not in result["result"]
    assert any("未返回可验证" in warning for warning in result["warnings"])
