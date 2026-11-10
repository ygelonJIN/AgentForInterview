"""Agent 启停与工具选择：可视化界面改这里，路由与工具注册按这里生效。

落盘在 ``data/agent_config.json``（生成物，不进版本库）：

```json
{"enabled": {"shopping": true},
 "tools": {"trip_gear": ["get_weather_forecast", "get_time_offset"]}}
```

读不到就当全部启用、每个 Agent 用默认白名单；删文件即可恢复默认。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "agent_config.json"


def _load() -> Dict[str, Any]:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save(payload: Dict[str, Any]) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception:
        # 工作区只读时不让写盘失败拖垮界面：内存里这次会话仍然生效。
        pass


def _enabled_map(payload: Dict[str, Any]) -> Dict[str, bool]:
    enabled = payload.get("enabled")
    if isinstance(enabled, dict):
        return {str(k): bool(v) for k, v in enabled.items() if isinstance(v, bool)}
    # 兼容最早一版：顶层直接是 {agent_id: bool}
    return {str(k): bool(v) for k, v in payload.items() if isinstance(v, bool)}


def load_enabled() -> Dict[str, bool]:
    return _enabled_map(_load())


def is_enabled(agent_id: str, default: bool = True) -> bool:
    return load_enabled().get(str(agent_id or "").strip().lower(), default)


def set_enabled(agent_id: str, enabled: bool) -> None:
    key = str(agent_id or "").strip().lower()
    payload = _load()
    mapping = _enabled_map(payload)
    mapping[key] = bool(enabled)
    payload.pop("enabled", None)
    for legacy_key in [k for k, v in payload.items() if isinstance(v, bool)]:
        payload.pop(legacy_key, None)
    payload["enabled"] = mapping
    _save(payload)


def tools_for(agent_id: str) -> Optional[List[str]]:
    """该 Agent 自定义的工具清单；没配置过返回 None（用默认白名单）。"""
    tools = _load().get("tools")
    if not isinstance(tools, dict):
        return None
    names = tools.get(str(agent_id or "").strip().lower())
    if names is None:
        return None
    if not isinstance(names, list):
        return None
    return [str(name) for name in names if str(name).strip()]


def set_tools(agent_id: str, names: List[str]) -> None:
    key = str(agent_id or "").strip().lower()
    payload = _load()
    tools = payload.get("tools")
    if not isinstance(tools, dict):
        tools = {}
    tools[key] = [str(name) for name in names if str(name).strip()]
    payload["tools"] = tools
    _save(payload)


def reset() -> None:
    try:
        CONFIG_PATH.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
