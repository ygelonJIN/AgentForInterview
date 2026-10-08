"""旅游计划步骤的真实工具执行器。"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional


class TravelToolExecutor:
    """按行程步骤调用天气、路线，并读取本地活动资料。"""

    def __init__(
        self,
        *,
        weather_tool: Optional[Callable[..., Any]] = None,
        route_tool: Optional[Callable[..., Any]] = None,
        activities_dir: Optional[Path] = None,
    ):
        if weather_tool is None or route_tool is None:
            from app.tools.map import get_route
            from app.tools.weather import get_weather

            weather_tool = weather_tool or get_weather
            route_tool = route_tool or get_route
        self.weather_tool = weather_tool
        self.route_tool = route_tool
        self.activities_dir = (
            Path(activities_dir)
            if activities_dir
            else Path(__file__).resolve().parents[2] / "data" / "activities"
        )

    @staticmethod
    async def _invoke(tool: Callable[..., Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
        if hasattr(tool, "ainvoke"):
            result = await tool.ainvoke(dict(payload))
        elif hasattr(tool, "invoke"):
            result = await asyncio.to_thread(tool.invoke, dict(payload))
        else:
            result = tool(**dict(payload))
        return dict(result) if isinstance(result, dict) else {"result": str(result)}

    @classmethod
    async def _invoke_real(
        cls,
        tool: Callable[..., Any],
        payload: Mapping[str, Any],
        tool_name: str,
    ) -> Dict[str, Any]:
        """只接受真实 provider 结果；失败结果不进入计划证据。"""
        try:
            result = await cls._invoke(tool, payload)
        except Exception as exc:
            return {
                "ok": False,
                "accepted": False,
                "error": f"{tool_name} 不可用: {type(exc).__name__}: {str(exc)[:200]}",
            }
        if result.get("simulated") or result.get("stale"):
            return {
                **result,
                "ok": False,
                "accepted": False,
                "error": f"{tool_name} 未返回可验证的当前真实数据",
            }
        if result.get("error"):
            return {**result, "ok": False, "accepted": False}
        return {**result, "ok": True, "accepted": True}

    async def get_weather_evidence(
        self,
        destination: str,
        travel_date: date,
    ) -> Dict[str, Any]:
        """获取只可作为事实使用的天气证据；失败或过期数据明确拒绝。"""
        return await self._invoke_real(
            self.weather_tool,
            {"city": destination, "date": travel_date.isoformat()},
            "get_weather",
        )

    @staticmethod
    def _step_index(step: Mapping[str, Any]) -> int:
        match = re.search(r"(?:day-|第)(\d+)", str(step.get("id", "")))
        return max(1, int(match.group(1))) if match else 1

    @staticmethod
    def _start_date(requirements: Mapping[str, Any]) -> date:
        raw = str(requirements.get("start_date") or "").strip()
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
        return date.today()

    @staticmethod
    def _route_pair(text: str) -> Optional[tuple[str, str]]:
        match = re.search(r"从\s*([^，。；\n]+?)\s*(?:到|去|前往)\s*([^，。；\n]+)", text)
        if not match:
            return None
        origin, destination = (part.strip() for part in match.groups())
        return (origin, destination) if origin and destination and origin != destination else None

    def _activities(self, destination: str) -> str:
        candidates = [
            self.activities_dir / f"{destination}_activities.txt",
            self.activities_dir / "outdoor_activities.txt",
        ]
        for path in candidates:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text[:1200]
        return ""

    async def execute(self, step: Mapping[str, Any], state: Mapping[str, Any]) -> Dict[str, Any]:
        requirements = dict(state.get("requirements") or {})
        destination = str(requirements.get("destination") or "").strip()
        if not destination:
            raise ValueError("缺少目的地，无法执行天气和路线工具")

        text = f"{step.get('title', '')}\n{step.get('description', '')}"
        step_index = self._step_index(step)
        travel_date = self._start_date(requirements) + timedelta(days=step_index - 1)
        tool_calls: List[Dict[str, Any]] = []
        warnings: List[str] = []
        simulated = False
        summaries: List[str] = []

        weather = await self._invoke_real(
            self.weather_tool,
            {"city": destination, "date": travel_date.isoformat()},
            "get_weather",
        )
        tool_calls.append({
            "tool": "get_weather",
            "ok": bool(weather.get("accepted")),
            "result": weather,
        })
        if weather.get("simulated"):
            simulated = True
        if weather.get("accepted"):
            summaries.append(
                f"{travel_date.isoformat()} {destination}天气："
                f"{weather.get('temperature', '')} {weather.get('condition', '')}"
            )
        else:
            warnings.append(str(weather.get("error") or "天气数据不可用"))

        route_pair = self._route_pair(text)
        if route_pair:
            route = await self._invoke_real(
                self.route_tool,
                {
                    "origin": route_pair[0],
                    "destination": route_pair[1],
                    "mode": "driving",
                    "city": destination,
                },
                "get_route",
            )
            tool_calls.append({
                "tool": "get_route",
                "ok": bool(route.get("accepted")),
                "result": route,
            })
            if route.get("simulated"):
                simulated = True
            if route.get("accepted"):
                summaries.append(
                    f"{route_pair[0]}到{route_pair[1]}："
                    f"{route.get('distance', '')} / {route.get('duration', '')}"
                )
            else:
                warnings.append(str(route.get("error") or "路线数据不可用"))

        activities = self._activities(destination)
        if activities:
            summaries.append(f"本地活动参考：\n{activities[:500]}")

        for item in tool_calls:
            result = item.get("result") or {}
            if result.get("warning"):
                warnings.append(str(result["warning"]))
            warnings.extend(str(warning) for warning in (result.get("warnings") or []))
        return {
            "status": "completed",
            "result": "\n".join(part for part in summaries if part.strip()),
            "source": "travel_tool_registry",
            "tool_calls": tool_calls,
            "simulated": simulated,
            "warnings": list(dict.fromkeys(warnings)),
            "executed_at": travel_date.isoformat(),
        }
