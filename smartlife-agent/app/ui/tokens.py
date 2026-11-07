"""设计 token（V2 · 暖纸手账）。

单一事实来源：前端原型的 `frontend-preview/tokens/theme-v2.css` 与本文件一一对应。
Streamlit 侧通过 `:root { --token: value }` 注入，CSS 只允许使用 `var(--token)`，
新增颜色/圆角/间距必须先在此登记，避免样式散落。

分四组：
- 颜色 `--c-*`：表面、文字、强调、状态、区域、组件
- 字体 `--font-* / --fs-* / --fw-* / --lh-* / --tracking-*`
- 形状与底板 `--radius-* / --shadow-* / --border-* / --space-*`
- 布局尺寸 `--nav-w / --content-max / ...`
"""
from __future__ import annotations

from typing import Dict, Iterable, Tuple

V2_TOKENS: Dict[str, str] = {
    # —— 表面与文字 ——
    "c-bg": "#f7f0e2",
    "c-surface": "#fffaf1",
    "c-surface-2": "#fbf4e6",
    "c-text": "#33291d",
    "c-text-muted": "#7c6a52",
    "c-text-faint": "#a8967c",
    "c-line": "#e2d4b9",
    "c-line-strong": "#cdb994",
    # —— 强调色 ——
    "c-accent": "#a8501f",
    "c-accent-strong": "#8a3f14",
    "c-accent-ink": "#fff7ec",
    "c-accent-soft": "#f7e7d3",
    "c-accent-line": "#e0bd97",
    "c-accent-text": "#8a3f14",
    # —— 状态色 ——
    "c-ok": "#4f6b32",
    "c-ok-soft": "#eef4e1",
    "c-ok-line": "#cfdcb2",
    "c-warn": "#9a6a12",
    "c-warn-soft": "#fbf1da",
    "c-warn-line": "#e8cf9a",
    "c-danger": "#9c3324",
    "c-danger-soft": "#fbe9e3",
    "c-danger-line": "#e8bdaf",
    # —— 导航 ——
    "c-nav-bg": "#f2e8d6",
    "c-nav-text": "#403322",
    "c-nav-muted": "#9a876b",
    "c-nav-hover-bg": "#ece0c9",
    "c-nav-active-bg": "#fffaf1",
    "c-nav-active-text": "#8a3f14",
    "c-nav-active-line": "#dcc9a6",
    # —— 区域 ——
    "c-topbar-bg": "rgba(247, 240, 226, 0.92)",
    "c-composer-bg": "rgba(247, 240, 226, 0.96)",
    "c-rail-bg": "#f4ebda",
    "c-panel-head-bg": "#fbf4e6",
    # —— 组件 ——
    "c-chip-bg": "#f6ecda",
    "c-chip-text": "#6d5940",
    "c-user-bubble-bg": "#ecdcc1",
    "c-user-bubble-text": "#3a2c1b",
    "c-user-bubble-meta": "#8a7357",
    "c-track-bg": "#efe3cd",
    "c-select-bg": "#c9862f",
    "c-select-text": "#2b2116",
    # —— 字体 ——
    "font-sans": '"PingFang SC", "Songti SC", "Noto Serif SC", Georgia, serif',
    "font-display": '"Songti SC", "Noto Serif SC", "Source Han Serif SC", Georgia, serif',
    "font-mono": '"SF Mono", "Menlo", "Courier New", monospace',
    "fs-2xs": "11px",
    "fs-xs": "12.5px",
    "fs-sm": "13.5px",
    "fs-md": "14.5px",
    "fs-lg": "17px",
    "fs-xl": "24px",
    "fs-2xl": "28px",
    "lh-normal": "1.72",
    "lh-tight": "1.4",
    "fw-body": "400",
    "fw-medium": "500",
    "fw-semibold": "600",
    "fw-bold": "700",
    "tracking-body": "0.01em",
    "tracking-caps": "0.12em",
    "tracking-display": "0.005em",
    # —— 间距 / 形状 ——
    "space-1": "5px",
    "space-2": "9px",
    "space-3": "13px",
    "space-4": "18px",
    "space-5": "22px",
    "space-6": "30px",
    "space-7": "38px",
    "space-8": "54px",
    "radius-xs": "2px",
    "radius-sm": "4px",
    "radius-md": "6px",
    "radius-lg": "9px",
    "radius-pill": "999px",
    "border-w": "1px",
    "panel-rule-w": "1px",
    "shadow-1": "0 1px 0 rgba(122, 96, 62, 0.10)",
    "shadow-2": "0 1px 0 rgba(122, 96, 62, 0.08)",
    "shadow-3": "0 14px 30px rgba(122, 96, 62, 0.14)",
    "glow": "0 0 0 3px rgba(168, 80, 31, 0.14)",
    # —— 布局尺寸 ——
    "nav-w": "236px",
    "page-head-h": "168px",
    "rail-w": "292px",
    "content-max": "880px",
    "row-source-w": "126px",
    "budget-side-w": "212px",
    "avatar-size": "30px",
    "mark-size": "32px",
    "bar-h": "9px",
    "day-min": "288px",
    "bubble-radius": "10px",
    "bubble-tail": "2px",
    "timeline-w": "1px",
    "timeline-dot": "6px",
    # —— 底纹（纸面颗粒）——
    "bg-texture": (
        "radial-gradient(rgba(150, 118, 74, 0.10) 0.8px, transparent 0.9px),"
        " radial-gradient(rgba(150, 118, 74, 0.06) 0.8px, transparent 0.9px)"
    ),
    "bg-texture-size": "7px 7px, 11px 11px",
}

TOKEN_GROUPS: Tuple[Tuple[str, Iterable[str]], ...] = (
    ("表面与文字", ("c-bg", "c-surface", "c-surface-2", "c-text", "c-text-muted", "c-text-faint", "c-line", "c-line-strong")),
    ("强调色", ("c-accent", "c-accent-strong", "c-accent-ink", "c-accent-soft", "c-accent-line", "c-accent-text")),
    ("状态色", ("c-ok", "c-ok-soft", "c-ok-line", "c-warn", "c-warn-soft", "c-warn-line", "c-danger", "c-danger-soft", "c-danger-line")),
    ("导航", ("c-nav-bg", "c-nav-text", "c-nav-muted", "c-nav-hover-bg", "c-nav-active-bg", "c-nav-active-text", "c-nav-active-line")),
    ("区域与组件", ("c-topbar-bg", "c-composer-bg", "c-rail-bg", "c-panel-head-bg", "c-chip-bg", "c-chip-text", "c-user-bubble-bg", "c-user-bubble-text", "c-user-bubble-meta", "c-track-bg", "c-select-bg", "c-select-text")),
    ("字体", ("font-sans", "font-display", "font-mono", "fs-2xs", "fs-xs", "fs-sm", "fs-md", "fs-lg", "fs-xl", "fs-2xl", "lh-normal", "lh-tight", "fw-body", "fw-medium", "fw-semibold", "fw-bold", "tracking-body", "tracking-caps", "tracking-display")),
    ("间距与形状", ("space-1", "space-2", "space-3", "space-4", "space-5", "space-6", "space-7", "space-8", "radius-xs", "radius-sm", "radius-md", "radius-lg", "radius-pill", "border-w", "panel-rule-w", "shadow-1", "shadow-2", "shadow-3", "glow")),
    ("布局尺寸", ("nav-w", "page-head-h", "rail-w", "content-max", "row-source-w", "budget-side-w", "avatar-size", "mark-size", "bar-h", "day-min", "bubble-radius", "bubble-tail", "timeline-w", "timeline-dot")),
    ("底纹", ("bg-texture", "bg-texture-size")),
)


def token(name: str) -> str:
    """取单个 token 的值，未知 token 直接报错，防止拼错变量名。"""
    try:
        return V2_TOKENS[name]
    except KeyError as exc:  # pragma: no cover - 开发期防护
        raise KeyError(f"未登记的 design token: --{name}") from exc


def css_variables() -> str:
    """渲染 `:root` 变量块，供 CSS 与组件共用。"""
    lines = [f"  --{name}: {value};" for name, value in V2_TOKENS.items()]
    return ":root {\n" + "\n".join(lines) + "\n}"
