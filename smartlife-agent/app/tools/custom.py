"""全局工具池：主仓内置工具 + 用户自己加的工具。

内置工具在 ``app/tools`` 里；用户自己的工具放 ``data/tools/*.py``，文件里定义
``TOOLS = [@tool ...]`` 即可，启动时自动加载进全局池，然后在「Agent 配置」页
勾选给某个 Agent。

自定义工具是**本地代码**，会被执行——只放你自己写的文件。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

CUSTOM_DIR = Path(__file__).resolve().parents[2] / "data" / "tools"

_cache: Optional[Dict[str, Any]] = None


def _reset() -> None:
    global _cache
    _cache = None


def load_custom_tools() -> Dict[str, Any]:
    """扫描 data/tools/*.py，取每个模块的 TOOLS 列表；坏文件跳过。"""
    global _cache
    if _cache is not None:
        return _cache
    tools: Dict[str, Any] = {}
    if not CUSTOM_DIR.exists():
        _cache = tools
        return tools
    for path in sorted(CUSTOM_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"smartlife_custom_tool_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            (path.with_suffix(".broken")).write_text(
                f"{type(exc).__name__}: {exc}", encoding="utf-8"
            )
            continue
        for tool in list(getattr(module, "TOOLS", []) or []):
            name = str(getattr(tool, "name", "") or "")
            if name:
                tools.setdefault(name, tool)
    _cache = tools
    return tools


def custom_tool_names() -> List[str]:
    return sorted(load_custom_tools())


def register_custom_tools(register: Any) -> None:
    """把自定义工具交给注册表；名字与内置冲突时保留内置。"""
    for tool in load_custom_tools().values():
        register(tool)
