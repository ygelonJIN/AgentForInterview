"""可复用性护栏：身份服务不允许依赖本项目代码或 Streamlit。

这条测试比文档更管用——半年后有人顺手 ``from app.config import ...``，
这里会直接失败。
"""
from __future__ import annotations

import ast
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("app", "streamlit")


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def test_service_has_no_project_or_streamlit_imports() -> None:
    offenders: list[str] = []
    for path in SERVICE_ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for module in _imported_modules(path):
            root = module.split(".")[0]
            if root in FORBIDDEN:
                offenders.append(f"{path.relative_to(SERVICE_ROOT)}: {module}")
    assert not offenders, "身份服务必须保持通用，发现项目依赖: " + ", ".join(offenders)
