"""旅游计划步骤的真实工具执行器。"""

from __future__ import annotations

import asyncio
import re
import time
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
        activities_tool: Optional[Callable[..., Any]] = None,
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
        self._activities_tool_instance = activities_tool

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
        started = time.perf_counter()

        def elapsed_ms() -> float:
            return round((time.perf_counter() - started) * 1000, 3)

        try:
            result = await cls._invoke(tool, payload)
        except Exception as exc:
            return {
                "ok": False,
                "accepted": False,
                "error": f"{tool_name} 不可用: {type(exc).__name__}: {str(exc)[:200]}",
                "duration_ms": elapsed_ms(),
            }
        if result.get("simulated") or result.get("stale"):
            return {
                **result,
                "ok": False,
                "accepted": False,
                "error": f"{tool_name} 未返回可验证的当前真实数据",
                "duration_ms": elapsed_ms(),
            }
        if result.get("error"):
            return {**result, "ok": False, "accepted": False, "duration_ms": elapsed_ms()}
        return {**result, "ok": True, "accepted": True, "duration_ms": elapsed_ms()}

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

    def _activities_tool(self) -> Optional[Callable[..., Any]]:
        """取 ``get_local_activities`` 工具；只构造一次。"""
        if self._activities_tool_instance is None:
            try:
                from app.tools.travel_tools import TravelToolProvider

                provider = TravelToolProvider(self.activities_dir)
                self._activities_tool_instance = provider.get_tool_by_name("get_local_activities")
            except Exception:
                self._activities_tool_instance = None
        return self._activities_tool_instance

    @staticmethod
    def _activities_summary(items: List[Mapping[str, Any]]) -> str:
        """按"能不能参加"分组，而不是把场所和活动混成一张清单。"""

        def render(rows: List[Mapping[str, Any]], title: str) -> str:
            lines = []
            for index, item in enumerate(rows[:6], start=1):
                text = str(item.get("title") or "").strip()
                kind = str(item.get("kind") or "").strip()
                if not text:
                    continue
                lines.append(f"{index}. {text}（{kind}）" if kind else f"{index}. {text}")
            return f"{title}\n" + "\n".join(lines) if lines else ""

        joiner = [item for item in items if item.get("participatory")]
        places = [item for item in items if not item.get("participatory")]
        blocks = [
            block for block in (
                render(joiner, "可以买票或报名参加的活动场所："),
                render(places, "另外可去的地方："),
            ) if block
        ]
        return "\n".join(blocks)

    async def activities_evidence(
        self,
        destination: str,
        travel_date: date,
    ) -> Dict[str, Any]:
        """活动证据：全球 provider 优先；拿不到就明确不可用，绝不跨城市凑数。"""
        tool = self._activities_tool()
        if tool is None:
            return {
                "available": False,
                "source": "unavailable",
                "items": [],
                "error": "活动工具未注册",
            }
        payload = {"city": destination, "date": travel_date.isoformat()}
        started = time.perf_counter()
        try:
            if hasattr(tool, "ainvoke"):
                raw = await tool.ainvoke(payload)
            else:
                raw = await asyncio.to_thread(tool.invoke, payload)
        except Exception as exc:
            return {
                "available": False,
                "source": "unavailable",
                "items": [],
                "error": f"get_local_activities 不可用: {type(exc).__name__}: {str(exc)[:200]}",
            }

        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        row = raw[0] if isinstance(raw, (list, tuple)) and raw else (raw if isinstance(raw, dict) else {})
        items = list(row.get("activities") or [])
        warnings = list(row.get("warnings") or [])
        available = bool(row.get("available")) and bool(items)
        summary = self._activities_summary(items)
        if available:
            summary = f"来源：{row.get('source', '')}\n{summary}"
            if row.get("note"):
                summary += f"\n（{row['note']}）"
        return {
            "available": available,
            "source": str(row.get("source") or "unavailable"),
            "kind": str(row.get("kind") or ""),
            "items": items,
            "summary": summary,
            "note": str(row.get("note") or ""),
            "stale": False,
            "duration_ms": duration_ms,
            "warnings": warnings,
            "error": "" if available else "未获取到可验证的本地活动数据",
        }

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

        route_pair = self._route_pair(text)
        weather_task = asyncio.create_task(self._invoke_real(
            self.weather_tool,
            {"city": destination, "date": travel_date.isoformat()},
            "get_weather",
        ))
        route_task = (
            asyncio.create_task(self._invoke_real(
                self.route_tool,
                {
                    "origin": route_pair[0],
                    "destination": route_pair[1],
                    "mode": "driving",
                    "city": destination,
                },
                "get_route",
            ))
            if route_pair
            else None
        )
        weather = await weather_task
        tool_calls.append({
            "tool": "get_weather",
            "ok": bool(weather.get("accepted")),
            "result": weather,
        })
        if weather.get("simulated"):
            simulated = True
        if weather.get("accepted"):
            summaries.append(
                f"第{step_index}天（{travel_date.isoformat()}）{destination}天气："
                f"{weather.get('temperature', '')} {weather.get('condition', '')}"
            )
        else:
            warnings.append(str(weather.get("error") or "天气数据不可用"))

        if route_task is not None and route_pair:
            route = await route_task
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

        activities = await self.activities_evidence(destination, travel_date)
        tool_calls.append({
            "tool": "get_local_activities",
            "ok": bool(activities.get("available")),
            "result": {
                "source": activities.get("source"),
                "available": activities.get("available"),
                "count": len(activities.get("items") or []),
                "stale": activities.get("stale"),
                "simulated": False,
                "warnings": activities.get("warnings") or [],
                "error": activities.get("error") or "",
                "duration_ms": activities.get("duration_ms", 0.0),
            },
        })
        if activities.get("available"):
            summaries.append(
                f"第{step_index}天（{travel_date.isoformat()}）本地活动参考："
                f"\n{activities.get('summary', '')}"
            )
        warnings.extend(str(w) for w in (activities.get("warnings") or []))

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
