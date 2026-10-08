"""人工验证免费真实天气和路线工具的本地脚本。"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.map import get_route
from app.tools.providers import get_provider_status
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
