"""人工验证免费真实天气和路线工具的本地脚本。"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.geo import resolve_location
from app.tools.map import get_route
from app.tools.providers import get_provider_status
from app.tools.travel_tools import TravelToolProvider
from app.tools.weather import get_weather


def main() -> int:
    status = get_provider_status()
    results = {"provider_status": status}
    checks = {
        "weather": (
            get_weather,
            {"city": "杭州", "date": date.today().isoformat()},
        ),
        "route": (
            get_route,
            {
                "origin": "西湖",
                "destination": "灵隐寺",
                "mode": "driving",
                "city": "杭州",
            },
        ),
    }

    # 活动与地名解析：换成非中文城市，验证"世界各地随机问"这条路真的通
    activities_tool = TravelToolProvider().get_tool_by_name("get_local_activities")
    checks["activities"] = (
        activities_tool,
        {"city": "新加坡", "date": date.today().isoformat()},
    )

    for city in ("东京", "纽约", "圣托里尼"):
        try:
            location = resolve_location(city)
            results.setdefault("geocode", {})[city] = {
                "ok": not location.uncertain,
                "name": location.name,
                "country": location.country,
                "latitude": round(location.latitude, 3),
                "longitude": round(location.longitude, 3),
            }
        except Exception as exc:
            results.setdefault("geocode", {})[city] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    for name, (tool, payload) in checks.items():
        try:
            results[name] = {"ok": True, "data": tool.invoke(payload)}
        except Exception as exc:
            results[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("ok") for item in results.values() if isinstance(item, dict) and "ok" in item) else 1


if __name__ == "__main__":
    raise SystemExit(main())
