"""人工验证天气、路线和酒店工具的本地脚本。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.hotel import search_hotels
from app.tools.map import get_route
from app.tools.providers import get_provider_status
from app.tools.weather import get_weather


def main() -> None:
    status = get_provider_status()
    results = {
        "provider_status": status,
        "weather": get_weather.invoke({"city": "杭州", "date": "2026-10-05"}),
        "route": get_route.invoke({"origin": "西湖", "destination": "灵隐寺", "mode": "driving"}),
        "hotel": search_hotels.invoke({
            "city": "杭州",
            "check_in": "2026-10-05",
            "check_out": "2026-10-07",
        }),
    }
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
