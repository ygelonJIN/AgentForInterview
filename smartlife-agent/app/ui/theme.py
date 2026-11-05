"""全局视觉规范与共用展示组件（V2 · 暖纸手账）。

改版说明（2026-11）：
- 视觉全部来自 ``app/ui/tokens.py`` 的 token，本文件不写死颜色/圆角/字号。
- 结构对齐 ``frontend-preview/template.html``：左导航、主列 + 右轨、编号面板。
- Streamlit 原生控件（按钮/输入/聊天/展开器/标签页）统一在本文件重绘，
  页面层只负责编排，不再各自写样式。

组件命名约定：``render_*`` 输出 HTML；``*_html`` 只拼字符串（可复用于流式渲染）。
"""
from __future__ import annotations

from html import escape
from typing import Any, Dict, Iterable, List, Optional, Sequence

import streamlit as st

from app.ui.tokens import css_variables

# 导航：图标、文案、页面 key。与前端原型保持一致。
NAV_ITEMS = (
    ("◈", "智能助手", "assistant"),
    ("▤", "生活目录", "catalog"),
    ("◇", "记忆中心", "memory"),
    ("⇪", "数据导入", "import"),
    ("☰", "个人中心", "profile"),
)

STATUS_LABEL = {
    "ok": "满足",
    "violated": "违反",
    "relaxed": "已放宽",
    "unverified": "未校验",
}

GEAR_STATUS_LABEL = {
    "owned": "已有",
    "need_buy": "待购",
    "self_provided": "自备",
    "skipped": "已省略",
}


def _e(value: Any) -> str:
    return escape(str(value if value is not None else ""))


def _money(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "¥0"
    if abs(number - round(number)) < 0.005:
        return f"¥{int(round(number))}"
    return f"¥{number:.2f}"


# ============================================================
# 样式表
# ============================================================
THEME_CSS = """
{root}

/* ---------- 基础 ---------- */
html, body, [class*="css"], .stApp {{
  font-family: var(--font-sans);
  color: var(--c-text);
  font-size: var(--fs-md);
  line-height: var(--lh-normal);
  letter-spacing: var(--tracking-body);
  -webkit-font-smoothing: antialiased;
}}
.stApp {{
  background-color: var(--c-bg);
  background-image: var(--bg-texture);
  background-size: var(--bg-texture-size);
}}
h1, h2, h3, h4 {{ font-family: var(--font-display); font-weight: var(--fw-bold); letter-spacing: var(--tracking-display); }}
p {{ line-height: var(--lh-normal); }}
::selection {{ background: var(--c-select-bg); color: var(--c-select-text); }}

#MainMenu, footer, [data-testid="stMainMenuButton"], [data-testid="stBaseButton-header"] {{ visibility: hidden !important; }}
[data-testid="stSkillsNudgeAnchor"] {{ display: none !important; }}
[data-testid="stHeader"] {{
  height: 0 !important; min-height: 0 !important; background: transparent !important;
  overflow: visible !important; z-index: 120;
}}
[data-testid="stSidebarHeader"] {{
  height: 0 !important; min-height: 0 !important; padding: 0 !important; margin: 0 !important;
  overflow: visible !important; position: relative;
}}
/* 侧栏收起/展开：头部高度归零后必须自己定位，否则按钮被裁掉 */
[data-testid="stSidebarCollapseButton"] {{ position: absolute !important; top: 12px; right: 10px; display: block !important; z-index: 130; }}
[data-testid="stExpandSidebarButton"] {{ position: fixed !important; top: 12px; left: 10px; display: block !important; z-index: 130; }}
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarCollapseButton"] button,
[data-testid="stExpandSidebarButton"],
[data-testid="stExpandSidebarButton"] button {{
  visibility: visible !important; opacity: 1 !important;
}}
[data-testid="stSidebarCollapseButton"] button,
[data-testid="stExpandSidebarButton"] button {{
  width: 28px; height: 28px; padding: 0; border: 0; border-radius: var(--radius-sm);
  background: transparent; color: var(--c-nav-muted);
  display: inline-flex !important; align-items: center; justify-content: center;
}}
[data-testid="stExpandSidebarButton"] button {{
  background: var(--c-surface) !important;
  border: var(--border-w) solid var(--c-line) !important;
  box-shadow: var(--shadow-1) !important;
}}
[data-testid="stSidebarCollapseButton"] button:hover,
[data-testid="stExpandSidebarButton"] button:hover {{ background: var(--c-nav-hover-bg) !important; }}

/* ---------- 页面骨架 ---------- */
[data-testid="stAppViewContainer"] {{ background: transparent; }}
[data-testid="stMainBlockContainer"] {{
  max-width: 1180px;
  padding: var(--space-5) var(--space-6) 9.5rem;
}}
[data-testid="stMainBlockContainer"] > div {{ gap: var(--space-4); }}
[data-testid="stVerticalBlock"] {{ gap: var(--space-3); }}
[data-testid="stHorizontalBlock"] {{ gap: var(--space-4); }}

/* ---------- 左导航 ---------- */
section[data-testid="stSidebar"] {{
  background: var(--c-nav-bg) !important;
  border-right: var(--border-w) solid var(--c-line);
  width: var(--nav-w) !important;
  min-width: var(--nav-w) !important;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
  padding: var(--space-5) var(--space-4) var(--space-5) !important;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] {{
  min-height: calc(100vh - 4rem);
}}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"]:last-child {{
  margin-top: auto;
}}
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: var(--space-2); }}
section[data-testid="stSidebar"] .stButton > button {{
  width: 100%;
  min-height: 38px;
  justify-content: flex-start;
  text-align: left;
  padding: var(--space-2) var(--space-3) !important;
  border: var(--border-w) solid transparent !important;
  border-radius: var(--radius-sm) !important;
  background: transparent !important;
  color: var(--c-nav-text) !important;
  font-size: var(--fs-sm) !important;
  font-weight: var(--fw-medium) !important;
  box-shadow: none !important;
  transform: none !important;
}}
section[data-testid="stSidebar"] .stButton > button:hover {{
  background: var(--c-nav-hover-bg) !important;
  color: var(--c-nav-active-text) !important;
}}
section[data-testid="stSidebar"] .stButton > button > div {{ width: 100%; justify-content: flex-start !important; gap: var(--space-2) !important; }}
section[data-testid="stSidebar"] .stButton > button p {{ white-space: nowrap; }}
section[data-testid="stSidebar"] .stButton > button[kind="primary"],
section[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {{
  background: var(--c-nav-active-bg) !important;
  border-color: var(--c-nav-active-line) !important;
  color: var(--c-nav-active-text) !important;
  font-weight: var(--fw-semibold) !important;
  box-shadow: var(--shadow-1) !important;
}}
section[data-testid="stSidebar"] [data-testid="stAlert"] {{ border-radius: var(--radius-sm); }}

.st-key-nav_menu > [data-testid="stElementContainer"]:nth-child(1) .stButton button::before {{ content: "◈"; }}
.st-key-nav_menu > [data-testid="stElementContainer"]:nth-child(2) .stButton button::before {{ content: "▤"; }}
.st-key-nav_menu > [data-testid="stElementContainer"]:nth-child(3) .stButton button::before {{ content: "◇"; }}
.st-key-nav_menu > [data-testid="stElementContainer"]:nth-child(4) .stButton button::before {{ content: "⇪"; }}
.st-key-nav_menu > [data-testid="stElementContainer"]:nth-child(5) .stButton button::before {{ content: "☰"; }}
.st-key-nav_menu .stButton button::before {{
  display: inline-block; width: 18px; margin-right: var(--space-2);
  text-align: center; color: var(--c-nav-muted); font-family: var(--font-sans);
  font-size: var(--fs-sm); font-weight: 400;
}}
.st-key-nav_menu .stButton button[kind="primary"]::before {{ color: var(--c-nav-active-text); }}

.sl-brand {{ display: flex; align-items: center; gap: var(--space-3); padding: 0 var(--space-1) var(--space-4); }}
.sl-mark {{
  display: grid; place-items: center; width: var(--mark-size); height: var(--mark-size);
  border-radius: var(--radius-sm); background: var(--c-accent); color: var(--c-accent-ink);
  font-family: var(--font-mono); font-size: var(--fs-sm); font-weight: var(--fw-bold);
}}
.sl-brand-name {{ font-family: var(--font-display); font-size: var(--fs-md); font-weight: var(--fw-bold); color: var(--c-nav-text); }}
.sl-brand-sub {{
  font-size: var(--fs-2xs); color: var(--c-nav-muted);
  letter-spacing: var(--tracking-caps); text-transform: uppercase;
}}
.sl-nav-label {{
  padding: var(--space-3) var(--space-3) var(--space-1);
  font-size: var(--fs-2xs); font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-caps); text-transform: uppercase;
  color: var(--c-nav-muted);
}}
.sl-thread {{
  display: flex; flex-direction: column; gap: 2px;
  padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm);
}}
.sl-thread-title {{ font-size: var(--fs-xs); color: var(--c-nav-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.sl-thread-meta {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-nav-muted); }}
.sl-thread--active {{ background: var(--c-nav-active-bg); border: var(--border-w) solid var(--c-nav-active-line); }}
.sl-user {{
  display: flex; align-items: center; gap: var(--space-3);
  margin-top: var(--space-4); padding-top: var(--space-4);
  border-top: var(--panel-rule-w) solid var(--c-nav-active-line);
}}
.sl-avatar {{
  display: grid; place-items: center; width: var(--avatar-size); height: var(--avatar-size);
  border-radius: var(--radius-pill); background: var(--c-chip-bg); color: var(--c-chip-text);
  font-family: var(--font-mono); font-size: var(--fs-2xs); font-weight: var(--fw-bold);
}}
.sl-user-name {{ font-size: var(--fs-xs); font-weight: var(--fw-semibold); color: var(--c-nav-text); }}
.sl-user-role {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-nav-muted); }}

/* ---------- 页头 ---------- */
.sl-page-head {{
  display: flex; align-items: flex-end; gap: var(--space-4);
  padding-bottom: var(--space-3); border-bottom: var(--panel-rule-w) solid var(--c-line);
  margin-bottom: var(--space-1);
}}
.sl-page-head .sl-kicker {{
  font-size: var(--fs-2xs); font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-accent-text);
  margin-bottom: var(--space-1);
}}
.sl-page-head h1 {{
  margin: 0; font-size: var(--fs-2xl); line-height: 1.25;
  letter-spacing: var(--tracking-display); color: var(--c-text);
}}
.sl-page-head p {{
  margin: var(--space-2) 0 0; max-width: 62ch;
  font-size: var(--fs-sm); color: var(--c-text-muted);
}}
.sl-page-meta {{ margin-left: auto; display: flex; gap: var(--space-2); flex-wrap: wrap; padding-bottom: 2px; }}

.sl-section {{
  display: flex; align-items: baseline; gap: var(--space-3);
  margin: var(--space-3) 0 var(--space-1);
}}
.sl-section h2 {{ margin: 0; font-size: var(--fs-lg); color: var(--c-text); }}
.sl-section p {{ margin: 0; font-size: var(--fs-xs); color: var(--c-text-muted); }}
.sl-section .sl-section-rule {{ flex: 1; height: 0; border-top: var(--panel-rule-w) solid var(--c-line); }}

/* ---------- 面板 ---------- */
.sl-panel {{
  background: var(--c-surface);
  border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-2);
  overflow: hidden;
}}
.sl-panel + .sl-panel {{ margin-top: var(--space-3); }}
.sl-panel-head {{
  display: flex; align-items: baseline; gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-bottom: var(--panel-rule-w) solid var(--c-line);
  background: var(--c-panel-head-bg);
}}
.sl-panel-idx {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); letter-spacing: var(--tracking-caps); }}
.sl-panel-title {{ font-family: var(--font-display); font-size: var(--fs-md); font-weight: var(--fw-bold); letter-spacing: var(--tracking-display); }}
.sl-panel-meta {{ margin-left: auto; font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); }}
.sl-panel-body {{ padding: var(--space-4) var(--space-5); }}
.sl-panel-body--flush {{ padding: 0; }}

/* ---------- 标签 ---------- */
.sl-tag {{
  display: inline-flex; align-items: center; justify-content: center;
  padding: 1px var(--space-2); border-radius: var(--radius-xs);
  border: var(--border-w) solid var(--c-line); background: var(--c-chip-bg); color: var(--c-chip-text);
  font-family: var(--font-mono); font-size: var(--fs-2xs); letter-spacing: 0.04em; white-space: nowrap;
}}
.sl-tag--hard {{ background: var(--c-accent-soft); border-color: var(--c-accent-line); color: var(--c-accent-text); }}
.sl-tag--soft {{ background: var(--c-surface-2); color: var(--c-text-muted); }}
.sl-tag--ok {{ background: var(--c-ok-soft); border-color: var(--c-ok-line); color: var(--c-ok); }}
.sl-tag--warn {{ background: var(--c-warn-soft); border-color: var(--c-warn-line); color: var(--c-warn); }}
.sl-tag--danger {{ background: var(--c-danger-soft); border-color: var(--c-danger-line); color: var(--c-danger); }}
.sl-tag--muted {{ color: var(--c-text-faint); }}
.sl-tag--pill {{ border-radius: var(--radius-pill); }}

/* ---------- 约束 / 通用表格 ---------- */
.sl-rows {{ display: flex; flex-direction: column; }}
.sl-row {{
  display: grid; grid-template-columns: minmax(0, 1fr) var(--row-source-w) 64px 84px;
  align-items: center; gap: var(--space-4);
  padding: var(--space-3) var(--space-5);
  border-bottom: var(--panel-rule-w) solid var(--c-line);
  font-size: var(--fs-sm);
}}
.sl-row:last-child {{ border-bottom: 0; }}
.sl-row--head {{
  font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase;
  color: var(--c-text-faint); background: var(--c-surface-2); font-weight: var(--fw-bold);
}}
.sl-row-label {{ min-width: 0; font-weight: var(--fw-medium); }}
.sl-row-label small {{ display: block; font-size: var(--fs-2xs); font-weight: 400; color: var(--c-text-faint); }}
.sl-row-src {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); }}

/* ---------- 行程 ---------- */
.sl-days {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--day-min), 1fr)); gap: var(--space-4); }}
.sl-day {{ border: var(--border-w) solid var(--c-line); border-radius: var(--radius-md); background: var(--c-surface-2); padding: var(--space-4); }}
.sl-day-head {{ display: flex; align-items: baseline; gap: var(--space-3); margin-bottom: var(--space-3); }}
.sl-day-no {{ font-family: var(--font-mono); font-size: var(--fs-xs); font-weight: var(--fw-bold); color: var(--c-accent); letter-spacing: var(--tracking-caps); }}
.sl-day-name {{ font-size: var(--fs-sm); font-weight: var(--fw-semibold); }}
.sl-day-sum {{ margin-left: auto; font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); }}
.sl-tl {{ display: flex; flex-direction: column; gap: var(--space-2); }}
.sl-tl-item {{
  display: grid; grid-template-columns: 52px minmax(0, 1fr) auto; gap: var(--space-3);
  align-items: baseline; position: relative; padding-left: var(--space-3);
  border-left: var(--timeline-w) solid var(--c-line-strong);
}}
.sl-tl-item::before {{
  content: ""; position: absolute; top: 0.5em; left: calc(-1 * (var(--timeline-w) + var(--timeline-dot) / 2));
  width: var(--timeline-dot); height: var(--timeline-dot); border-radius: var(--radius-pill); background: var(--c-accent);
}}
.sl-tl-time {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); font-variant-numeric: tabular-nums; }}
.sl-tl-act {{ font-size: var(--fs-sm); min-width: 0; }}
.sl-tl-act small {{ display: block; font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); }}
.sl-tl-cost {{ font-family: var(--font-mono); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }}

/* ---------- 装备 ---------- */
.sl-gear {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-4); }}
.sl-gear-col {{ border: var(--border-w) solid var(--c-line); border-radius: var(--radius-md); overflow: hidden; }}
.sl-gear-head {{
  display: flex; align-items: baseline; gap: var(--space-2);
  padding: var(--space-2) var(--space-4); background: var(--c-surface-2);
  border-bottom: var(--panel-rule-w) solid var(--c-line);
  font-size: var(--fs-2xs); font-weight: var(--fw-bold); letter-spacing: var(--tracking-caps);
  text-transform: uppercase; color: var(--c-text-muted);
}}
.sl-gear-head .n {{ margin-left: auto; font-family: var(--font-mono); color: var(--c-text); }}
.sl-gear-row {{
  display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px var(--space-3);
  padding: var(--space-2) var(--space-4); border-bottom: var(--panel-rule-w) solid var(--c-line);
}}
.sl-gear-row:last-child {{ border-bottom: 0; }}
.sl-gear-name {{ font-size: var(--fs-sm); font-weight: var(--fw-medium); }}
.sl-gear-name .qty {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); margin-left: var(--space-2); }}
.sl-gear-reason {{ grid-column: 1 / -1; font-size: var(--fs-2xs); color: var(--c-text-faint); line-height: var(--lh-tight); }}
.sl-gear-price {{ font-family: var(--font-mono); font-size: var(--fs-xs); text-align: right; font-variant-numeric: tabular-nums; }}
.sl-gear-price small {{ display: block; font-size: var(--fs-2xs); color: var(--c-text-faint); }}
.sl-gear-foot {{ padding: var(--space-2) var(--space-4); font-size: var(--fs-2xs); color: var(--c-text-muted); background: var(--c-surface-2); border-top: var(--panel-rule-w) solid var(--c-line); }}

/* ---------- 预算 ---------- */
.sl-budget {{ display: grid; grid-template-columns: minmax(0, 1fr) var(--budget-side-w); gap: var(--space-6); align-items: center; }}
.sl-budget-nums {{ display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--space-3) var(--space-5); margin-bottom: var(--space-4); }}
.sl-budget-num span {{ display: block; font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-text-faint); }}
.sl-budget-num b {{ font-family: var(--font-mono); font-size: var(--fs-lg); font-variant-numeric: tabular-nums; }}
.sl-delta {{
  display: flex; flex-direction: column; align-items: flex-end; margin-left: auto;
  padding: var(--space-1) var(--space-3); border-radius: var(--radius-sm);
  border: var(--border-w) solid var(--c-ok-line); background: var(--c-ok-soft); color: var(--c-ok);
}}
.sl-delta span {{ font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; }}
.sl-delta b {{ font-family: var(--font-mono); font-size: var(--fs-md); font-variant-numeric: tabular-nums; }}
.sl-delta--over {{ border-color: var(--c-danger-line); background: var(--c-danger-soft); color: var(--c-danger); }}
.sl-bar {{ position: relative; height: var(--bar-h); border-radius: var(--radius-pill); background: var(--c-track-bg); border: var(--border-w) solid var(--c-line); overflow: hidden; }}
.sl-bar-fill {{ height: 100%; border-radius: inherit; background: var(--c-accent); }}
.sl-bar-fill--over {{ background: var(--c-danger); }}
.sl-bar-legend {{ display: flex; flex-wrap: wrap; gap: var(--space-4); margin-top: var(--space-2); font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); }}
.sl-budget-side {{ border-left: var(--panel-rule-w) solid var(--c-line); padding-left: var(--space-5); }}

.sl-kv {{ display: flex; flex-direction: column; gap: var(--space-2); }}
.sl-kv-row {{ display: flex; align-items: baseline; gap: var(--space-3); font-size: var(--fs-xs); }}
.sl-kv-row span {{ color: var(--c-text-muted); }}
.sl-kv-row b {{ margin-left: auto; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }}

/* ---------- 提示条 ---------- */
.sl-alert {{
  display: flex; gap: var(--space-3); align-items: flex-start;
  padding: var(--space-3) var(--space-4); border-radius: var(--radius-md);
  border: var(--border-w) solid var(--c-line); background: var(--c-surface);
  font-size: var(--fs-sm);
}}
.sl-alert + .sl-alert {{ margin-top: var(--space-2); }}
.sl-alert .ico {{ flex: none; font-family: var(--font-mono); }}
.sl-alert b {{ display: block; font-size: var(--fs-sm); margin-bottom: 2px; }}
.sl-alert p {{ margin: 0; font-size: var(--fs-xs); color: var(--c-text-muted); }}
.sl-alert--ok {{ background: var(--c-ok-soft); border-color: var(--c-ok-line); color: var(--c-ok); }}
.sl-alert--ok p {{ color: var(--c-ok); }}
.sl-alert--warn {{ background: var(--c-warn-soft); border-color: var(--c-warn-line); color: var(--c-warn); }}
.sl-alert--warn p {{ color: var(--c-warn); }}
.sl-alert--danger {{ background: var(--c-danger-soft); border-color: var(--c-danger-line); color: var(--c-danger); }}
.sl-alert--danger p {{ color: var(--c-danger); }}

/* ---------- 卡片 / 评价 / 指标 ---------- */
.sl-card {{
  height: 100%; padding: var(--space-4); border-radius: var(--radius-md);
  border: var(--border-w) solid var(--c-line); background: var(--c-surface);
  box-shadow: var(--shadow-2); display: flex; flex-direction: column; gap: var(--space-2);
}}
.sl-card-kicker {{ font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-text-faint); font-weight: var(--fw-bold); }}
.sl-card h3 {{ margin: 0; font-family: var(--font-display); font-size: var(--fs-md); }}
.sl-card p {{ margin: 0; font-size: var(--fs-xs); color: var(--c-text-muted); }}
.sl-card-meta {{ margin-top: auto; display: flex; flex-wrap: wrap; gap: var(--space-2); padding-top: var(--space-2); }}

.sl-review {{ padding: var(--space-3) var(--space-4); border-radius: var(--radius-md); border: var(--border-w) solid var(--c-line); background: var(--c-surface); }}
.sl-review + .sl-review {{ margin-top: var(--space-2); }}
.sl-review-head {{ display: flex; justify-content: space-between; gap: var(--space-3); font-size: var(--fs-xs); font-weight: var(--fw-semibold); }}
.sl-review-head span:last-child {{ font-family: var(--font-mono); color: var(--c-text-muted); font-weight: 400; }}
.sl-review-copy {{ margin-top: var(--space-1); font-size: var(--fs-xs); color: var(--c-text-muted); line-height: var(--lh-tight); }}

.sl-metric {{ padding: var(--space-3) var(--space-4); border: var(--border-w) solid var(--c-line); border-radius: var(--radius-md); background: var(--c-surface); }}
.sl-metric-label {{ font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-text-faint); }}
.sl-metric-value {{ font-family: var(--font-display); font-size: var(--fs-xl); line-height: 1.2; font-weight: var(--fw-bold); }}
.sl-metric-note {{ font-size: var(--fs-2xs); color: var(--c-text-muted); }}

.sl-empty {{
  padding: var(--space-6); text-align: center; font-size: var(--fs-sm); color: var(--c-text-muted);
  border: var(--border-w) dashed var(--c-line-strong); border-radius: var(--radius-md); background: var(--c-surface-2);
}}
.sl-empty--left {{ text-align: left; }}
.sl-empty-title {{ font-family: var(--font-display); font-size: var(--fs-md); font-weight: var(--fw-bold); color: var(--c-text); }}
.sl-empty p {{ margin: var(--space-2) 0 0; font-size: var(--fs-xs); line-height: var(--lh-normal); }}
.sl-empty-quote {{ font-family: var(--font-display); font-size: var(--fs-md) !important; color: var(--c-text) !important; }}

.sl-lead {{
  margin: 0 0 var(--space-4);
  font-family: var(--font-display);
  font-size: var(--fs-xl);
  line-height: 1.3;
  font-weight: var(--fw-semibold);
  letter-spacing: var(--tracking-display);
  color: var(--c-text);
}}
.sl-lead em {{ font-style: normal; color: var(--c-accent); }}
.sl-lead span {{ display: block; margin-top: var(--space-2); font-family: var(--font-sans); font-size: var(--fs-xs); font-weight: 400; color: var(--c-text-muted); }}

/* ---------- 右轨 ---------- */
.sl-rail {{ display: flex; flex-direction: column; gap: var(--space-3); }}
.st-key-assistant_rail {{
  position: sticky; top: var(--space-4);
}}
.sl-rail-block {{
  background: var(--c-rail-bg); border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-md); padding: var(--space-3) var(--space-4);
}}
.sl-rail-title {{
  display: flex; align-items: baseline; gap: var(--space-2);
  font-size: var(--fs-2xs); font-weight: var(--fw-bold); letter-spacing: var(--tracking-caps);
  text-transform: uppercase; color: var(--c-text-muted); margin-bottom: var(--space-2);
}}
.sl-rail-title em {{ margin-left: auto; font-family: var(--font-mono); font-style: normal; color: var(--c-text-faint); }}
.sl-steps {{ display: flex; flex-direction: column; }}
.sl-step {{ position: relative; display: grid; grid-template-columns: 16px minmax(0, 1fr) auto; gap: var(--space-2); padding: var(--space-1) 0; align-items: start; }}
.sl-step::after {{
  content: ""; position: absolute; left: 7px; top: 20px; bottom: -4px;
  width: var(--timeline-w); background: var(--c-line);
}}
.sl-step:last-child::after {{ display: none; }}
.sl-step-dot {{ width: 8px; height: 8px; margin-top: 6px; border-radius: var(--radius-pill); background: var(--c-surface); border: 1px solid var(--c-line-strong); z-index: 1; }}
.sl-step--done .sl-step-dot {{ background: var(--c-ok); border-color: var(--c-ok); }}
.sl-step--active .sl-step-dot {{ background: var(--c-accent); border-color: var(--c-accent); box-shadow: var(--glow); }}
.sl-step--cut .sl-step-dot {{ background: var(--c-warn); border-color: var(--c-warn); }}
.sl-step-name {{ font-size: var(--fs-xs); font-weight: var(--fw-medium); }}
.sl-step-desc {{ font-size: var(--fs-2xs); color: var(--c-text-faint); line-height: var(--lh-tight); }}
.sl-step-ms {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); }}
.sl-stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2) var(--space-3); }}
.sl-stat span {{ display: block; font-size: var(--fs-2xs); color: var(--c-text-faint); }}
.sl-stat b {{ font-family: var(--font-mono); font-size: var(--fs-sm); font-variant-numeric: tabular-nums; }}
.sl-rail-foot {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); line-height: var(--lh-tight); }}
.sl-rail-block .sl-rail-foot {{ margin-top: var(--space-2); }}

/* ---------- 聊天 ---------- */
[data-testid="stChatMessage"] {{
  background: transparent !important; border: 0 !important; padding: 0 !important;
  gap: 0 !important; margin-bottom: var(--space-5) !important;
}}
[data-testid="stChatMessage"] [data-testid^="stChatMessageAvatar"] {{ display: none !important; }}
[data-testid="stChatMessage"] [data-testid="stChatMessageContent"] {{ width: 100%; }}
[data-testid="stChatMessage"]:has([data-testid$="AvatarUser"]) {{ justify-content: flex-end; }}
[data-testid="stChatMessage"]:has([data-testid$="AvatarUser"]) [data-testid="stChatMessageContent"] {{
  max-width: 78%; margin-left: auto;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--bubble-radius); border-bottom-right-radius: var(--bubble-tail);
  background: var(--c-user-bubble-bg); color: var(--c-user-bubble-text);
  border: var(--border-w) solid var(--c-line); box-shadow: var(--shadow-2);
}}
[data-testid="stChatMessage"]:has([data-testid$="AvatarUser"]) p {{ margin: 0; font-size: var(--fs-md); color: var(--c-user-bubble-text); }}
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p {{ font-size: var(--fs-md); }}
[data-testid="stChatMessage"] h2 {{
  margin: var(--space-4) 0 var(--space-2); padding-top: var(--space-3);
  border-top: var(--panel-rule-w) solid var(--c-line);
  font-family: var(--font-display); font-size: var(--fs-lg);
}}
[data-testid="stChatMessage"] h2:first-child {{ margin-top: 0; border-top: 0; padding-top: 0; }}
[data-testid="stChatMessage"] h3 {{ margin: var(--space-3) 0 var(--space-1); font-size: var(--fs-md); }}
[data-testid="stChatMessage"] ul {{ padding-left: 1.1rem; }}
[data-testid="stChatMessage"] li {{ font-size: var(--fs-sm); margin: 2px 0; }}
[data-testid="stChatMessage"] hr {{ border-color: var(--c-line); }}

/* ---------- 输入区 ---------- */
[data-testid="stBottom"],
[data-testid="stBottom"] > div,
[data-testid="stBottomBlockContainer"] > div {{ background: transparent !important; }}
[data-testid="stBottomBlockContainer"] {{ max-width: 1180px; padding: 0 var(--space-6) var(--space-4) !important; }}
[data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] {{
  border: var(--border-w) solid var(--c-line-strong) !important;
  border-radius: var(--radius-lg) !important;
  background: var(--c-surface) !important;
  box-shadow: var(--shadow-1) !important;
  padding: var(--space-1) var(--space-2) !important;
}}
[data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] > div {{ background: transparent !important; border: 0 !important; }}
[data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] textarea {{
  background: transparent !important; color: var(--c-text) !important;
  font-family: var(--font-sans) !important; font-size: var(--fs-md) !important;
  caret-color: var(--c-accent);
}}
[data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] textarea::placeholder {{ color: var(--c-text-faint) !important; }}
[data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] button {{ color: var(--c-accent); }}
[data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] button:hover {{ background: var(--c-accent-soft) !important; }}
[data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] button svg {{ fill: currentColor; }}

/* ---------- 原生控件重绘 ---------- */
.stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] > button {{
  border-radius: var(--radius-sm);
  border: var(--border-w) solid var(--c-line-strong);
  background: var(--c-surface);
  color: var(--c-text);
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  min-height: 36px;
  padding: var(--space-2) var(--space-4);
  box-shadow: none;
  transition: background 140ms ease, border-color 140ms ease;
}}
.stButton > button:hover, .stDownloadButton > button:hover {{
  background: var(--c-surface-2);
  border-color: var(--c-accent-line);
  color: var(--c-accent-text);
}}
.stButton > button[kind="primary"] {{
  background: var(--c-accent-soft);
  border-color: var(--c-accent-line);
  color: var(--c-accent-text);
}}
.stButton > button[kind="primary"]:hover {{
  background: var(--c-accent);
  border-color: var(--c-accent);
  color: var(--c-accent-ink);
}}

/* 快捷提示词：chip 形态 */
.st-key-quick_chips [data-testid="stHorizontalBlock"] {{ gap: var(--space-2); }}
.st-key-quick_chips .stButton > button {{
  min-height: 30px; padding: 2px var(--space-3);
  border-radius: var(--radius-pill);
  background: var(--c-chip-bg); border-color: var(--c-line);
  color: var(--c-chip-text); font-size: var(--fs-2xs); font-weight: var(--fw-medium);
}}
.st-key-quick_chips .stButton > button:hover {{
  background: var(--c-accent-soft); border-color: var(--c-accent-line); color: var(--c-accent-text);
}}

.stTextInput input, .stNumberInput input, .stTextArea textarea, .stDateInput input {{
  border-radius: var(--radius-sm) !important;
  border: var(--border-w) solid var(--c-line-strong) !important;
  background: var(--c-surface) !important;
  color: var(--c-text) !important;
  font-family: var(--font-sans) !important;
  font-size: var(--fs-sm) !important;
}}
.stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus {{
  border-color: var(--c-accent) !important;
  box-shadow: var(--glow) !important;
}}
[data-testid="stWidgetLabel"] label, [data-testid="stWidgetLabel"] p {{
  font-size: var(--fs-xs) !important; color: var(--c-text-muted) !important;
  font-weight: var(--fw-medium) !important;
}}
.stSelectbox [data-baseweb="select"] > div, .stMultiSelect [data-baseweb="select"] > div {{
  border-radius: var(--radius-sm) !important;
  border-color: var(--c-line-strong) !important;
  background: var(--c-surface) !important;
  font-size: var(--fs-sm) !important;
}}
.stCheckbox label, .stRadio label {{ font-size: var(--fs-sm) !important; color: var(--c-text) !important; }}
[data-testid="stTabs"] [data-baseweb="tab-list"] {{ gap: var(--space-1); border-bottom: var(--panel-rule-w) solid var(--c-line); }}
[data-testid="stTabs"] [data-baseweb="tab"] {{
  padding: var(--space-2) var(--space-4); border-radius: 0;
  font-size: var(--fs-sm); color: var(--c-text-muted);
}}
[data-testid="stTabs"] [aria-selected="true"] {{ color: var(--c-accent-text); font-weight: var(--fw-semibold); }}
[data-testid="stTabs"] [data-baseweb="tab-highlight"] {{ background: var(--c-accent); }}

[data-testid="stExpander"] {{
  border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-md);
  background: var(--c-surface-2);
  overflow: hidden;
}}
[data-testid="stExpander"] summary {{ font-size: var(--fs-xs); font-weight: var(--fw-medium); color: var(--c-text-muted); padding: var(--space-2) var(--space-4); }}
[data-testid="stExpander"] summary:hover {{ color: var(--c-accent-text); }}
[data-testid="stExpander"] [data-testid="stExpanderDetails"] {{ padding: var(--space-2) var(--space-4) var(--space-3); }}

[data-testid="stAlert"] {{
  border-radius: var(--radius-md) !important;
  border: var(--border-w) solid var(--c-line) !important;
  border-left: 3px solid var(--c-accent) !important;
  background: var(--c-surface) !important;
  font-size: var(--fs-sm);
}}
[data-testid="stAlert"] [data-testid="stAlertContainer"] {{ background: transparent !important; }}
[data-testid="stAlert"] [data-testid^="stAlertContent"] {{ color: var(--c-text) !important; }}
[data-testid="stAlert"] p, [data-testid="stAlert"] span {{ color: var(--c-text) !important; font-size: var(--fs-sm) !important; }}
[data-testid="stAlert"] svg {{ color: var(--c-accent) !important; fill: var(--c-accent) !important; }}

/* 上传区与下拉 */
[data-testid="stFileUploaderDropzone"] {{
  background: var(--c-surface-2) !important;
  border: var(--border-w) dashed var(--c-line-strong) !important;
  border-radius: var(--radius-md) !important;
  padding: var(--space-4) !important;
}}
[data-testid="stFileUploaderDropzone"] span, [data-testid="stFileUploaderDropzone"] small {{ color: var(--c-text-muted) !important; }}
[data-baseweb="select"] > div {{
  background: var(--c-surface) !important;
  border-color: var(--c-line-strong) !important;
  border-radius: var(--radius-sm) !important;
}}
[data-baseweb="popover"] [data-baseweb="menu"] {{ background: var(--c-surface) !important; border: var(--border-w) solid var(--c-line) !important; }}
[data-baseweb="popover"] li {{ font-size: var(--fs-sm) !important; }}

/* 过程步骤：与右轨同构的紧凑时间线 */
.process-card {{
  margin: 0 0 2px;
  padding: 2px 0 2px var(--space-3);
  border-left: var(--timeline-w) solid var(--c-line-strong);
}}
.process-card .process-title {{
  font-family: var(--font-mono); font-size: var(--fs-2xs);
  letter-spacing: 0.06em; text-transform: uppercase; color: var(--c-text-faint);
}}
.process-card .process-copy {{ font-size: var(--fs-xs); color: var(--c-text-muted); line-height: var(--lh-tight); }}
.process-card.completed .process-title {{ color: var(--c-ok); }}
.process-card.warning .process-title {{ color: var(--c-warn); }}
.process-card.error .process-title {{ color: var(--c-danger); }}

/* 运行过程折叠条 */
.process-panel {{
  margin: 0 0 var(--space-4);
  border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-md);
  background: var(--c-surface-2);
  padding: var(--space-2) var(--space-3);
}}
.process-panel summary {{
  cursor: pointer; list-style: none;
  font-family: var(--font-mono); font-size: var(--fs-2xs);
  letter-spacing: 0.04em; color: var(--c-text-muted);
}}
.process-panel summary::-webkit-details-marker {{ display: none; }}
.process-panel summary::before {{ content: "＋ "; color: var(--c-text-faint); }}
.process-panel[open] summary::before {{ content: "－ "; }}
.process-panel > .process-card {{ margin-top: var(--space-2); }}

/* 执行日志折叠条 */
.execution-log-panel {{
  margin-top: var(--space-3);
  border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-md);
  background: var(--c-surface-2);
  padding: var(--space-2) var(--space-3);
}}
.execution-log-panel summary {{
  cursor: pointer; font-family: var(--font-mono); font-size: var(--fs-2xs);
  color: var(--c-text-muted); list-style: none;
}}
.execution-log-panel summary::-webkit-details-marker {{ display: none; }}
.execution-log-panel summary::before {{ content: "＋ "; color: var(--c-text-faint); }}
.execution-log-panel[open] summary::before {{ content: "－ "; }}
.execution-log-list {{ margin-top: var(--space-2); }}

/* 过程事件卡片（沿用 main.py 生成的类） */
.execution-log-row {{
  display: flex; gap: var(--space-3); padding: var(--space-2) var(--space-3);
  border-left: var(--timeline-w) solid var(--c-line-strong);
  font-size: var(--fs-xs);
}}
.execution-log-row + .execution-log-row {{ margin-top: 2px; }}
.execution-log-ok {{ color: var(--c-text-muted); }}
.execution-log-warn {{ border-left-color: var(--c-warn); color: var(--c-warn); }}
.execution-log-error {{ border-left-color: var(--c-danger); color: var(--c-danger); }}
.execution-log-icon {{ font-family: var(--font-mono); }}
.execution-log-meta {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); }}
.execution-log-message {{ font-size: var(--fs-xs); }}
.execution-log-detail {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); white-space: pre-wrap; }}

/* 数据表 */
[data-testid="stDataFrame"] {{ border: var(--border-w) solid var(--c-line); border-radius: var(--radius-md); }}

/* 滚动条 */
* {{ scrollbar-width: thin; }}
::-webkit-scrollbar {{ width: 9px; height: 9px; }}
::-webkit-scrollbar-thumb {{ background: var(--c-line-strong); border: 2px solid transparent; background-clip: content-box; border-radius: var(--radius-pill); }}
::-webkit-scrollbar-track {{ background: transparent; }}

/* ---------- 响应式 ---------- */
@media (max-width: 1200px) {{
  .sl-gear {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
}}
@media (max-width: 900px) {{
  [data-testid="stMainBlockContainer"] {{ padding-left: var(--space-4); padding-right: var(--space-4); }}
  [data-testid="stExpandSidebarButton"] {{ top: 10px; left: 10px; }}
  [data-testid="stMainBlockContainer"] {{ padding-top: 3.4rem; }}
  [data-testid="stBottomBlockContainer"] {{ padding-left: var(--space-4) !important; padding-right: var(--space-4) !important; }}
  .sl-gear {{ grid-template-columns: minmax(0, 1fr); }}
  .sl-budget {{ grid-template-columns: minmax(0, 1fr); }}
  .sl-budget-side {{ border-left: 0; border-top: var(--panel-rule-w) solid var(--c-line); padding-left: 0; padding-top: var(--space-4); }}
  .sl-row {{ grid-template-columns: minmax(0, 1fr) 64px; }}
  .sl-row > .sl-row-src, .sl-row > .sl-row-hard {{ display: none; }}
  .sl-page-head {{ flex-direction: column; align-items: flex-start; gap: var(--space-2); }}
  .sl-page-meta {{ margin-left: 0; }}
  .sl-page-head h1 {{ font-size: var(--fs-xl); }}
}}
"""


def _css() -> str:
    return THEME_CSS.format(root=css_variables())


def inject_theme() -> None:
    st.markdown(f"<style>{_css()}</style>", unsafe_allow_html=True)


# ============================================================
# 组件
# ============================================================
def render_page_header(title: str, description: str = "", meta: str = "", *, kicker: str = "") -> None:
    kicker_html = f'<div class="sl-kicker">{_e(kicker)}</div>' if kicker else ""
    meta_html = f'<div class="sl-tag sl-tag--pill">{_e(meta)}</div>' if meta else ""
    desc_html = f"<p>{_e(description)}</p>" if description else ""
    st.markdown(
        f"""
        <div class="sl-page-head">
          <div>{kicker_html}<h1>{_e(title)}</h1>{desc_html}</div>
          <div class="sl-page-meta">{meta_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_hero(title: str, description: str = "", *, kicker: str = "") -> None:
    """兼容旧调用：把原来的大 hero 收敛成页头。"""
    render_page_header(title, description, kicker=kicker or "SmartLife Agent")


def render_section_header(title: str, description: str = "") -> None:
    desc_html = f"<p>{_e(description)}</p>" if description else ""
    st.markdown(
        f'<div class="sl-section"><h2>{_e(title)}</h2>{desc_html}<span class="sl-section-rule"></span></div>',
        unsafe_allow_html=True,
    )


def tag(text: Any, tone: str = "") -> str:
    cls = f"sl-tag sl-tag--{tone}" if tone else "sl-tag"
    return f'<span class="{cls}">{_e(text)}</span>'


def alert_html(kind: str, title: str, body: str = "") -> str:
    icon = {"ok": "✓", "warn": "!", "danger": "×"}.get(kind, "·")
    cls = f"sl-alert sl-alert--{kind}" if kind else "sl-alert"
    body_html = f"<p>{_e(body)}</p>" if body else ""
    return (
        f'<div class="{cls}"><span class="ico">{icon}</span>'
        f"<div><b>{_e(title)}</b>{body_html}</div></div>"
    )


def render_alert(kind: str, title: str, body: str = "") -> None:
    st.markdown(alert_html(kind, title, body), unsafe_allow_html=True)


def panel_html(index: str, title: str, meta: str = "", body: str = "", *, flush: bool = False) -> str:
    meta_html = f'<span class="sl-panel-meta">{_e(meta)}</span>' if meta else ""
    body_cls = "sl-panel-body sl-panel-body--flush" if flush else "sl-panel-body"
    return (
        '<section class="sl-panel">'
        f'<div class="sl-panel-head"><span class="sl-panel-idx">{_e(index)}</span>'
        f'<span class="sl-panel-title">{_e(title)}</span>{meta_html}</div>'
        f'<div class="{body_cls}">{body}</div></section>'
    )


def render_panel(index: str, title: str, meta: str = "", body: str = "", *, flush: bool = False) -> None:
    st.markdown(panel_html(index, title, meta, body, flush=flush), unsafe_allow_html=True)


def render_metric(label: str, value: object, note: str = "") -> None:
    note_html = f'<div class="sl-metric-note">{_e(note)}</div>' if note else ""
    st.markdown(
        f'<div class="sl-metric"><div class="sl-metric-label">{_e(label)}</div>'
        f'<div class="sl-metric-value">{_e(value)}</div>{note_html}</div>',
        unsafe_allow_html=True,
    )


def metric_columns(items: Sequence[tuple], columns: int = 4):
    cols = st.columns(columns)
    for col, item in zip(cols, items):
        label, value, note = (list(item) + ["", ""])[:3]
        with col:
            render_metric(label, value, note)
    return cols


def render_card(
    title: str,
    *,
    kicker: str = "",
    description: str = "",
    meta: Iterable[str] = (),
    tone: str = "",
) -> None:
    kicker_html = f'<div class="sl-card-kicker">{_e(kicker)}</div>' if kicker else ""
    meta_html = "".join(tag(item, tone or "muted") for item in meta)
    st.markdown(
        f'<div class="sl-card">{kicker_html}<h3>{_e(title)}</h3>'
        f'<p>{_e(description)}</p><div class="sl-card-meta">{meta_html}</div></div>',
        unsafe_allow_html=True,
    )


def render_review(author: str, rating: int | float, content: str, product_name: Optional[str] = None) -> None:
    title = product_name or author
    right = f"{rating}/5 · {author}" if product_name else f"{rating}/5"
    st.markdown(
        f'<div class="sl-review"><div class="sl-review-head"><span>{_e(title)}</span>'
        f'<span>{_e(right)}</span></div><div class="sl-review-copy">{_e(content)}</div></div>',
        unsafe_allow_html=True,
    )


def render_empty(message: str) -> None:
    st.markdown(f'<div class="sl-empty">{_e(message)}</div>', unsafe_allow_html=True)


# ---------- 导航与右轨 ----------
def brand_html() -> str:
    return (
        '<div class="sl-brand"><div class="sl-mark">SL</div>'
        '<div><div class="sl-brand-name">SmartLife</div>'
        '<div class="sl-brand-sub">Trip &amp; Gear</div></div></div>'
    )


def nav_label_html(text: str) -> str:
    return f'<div class="sl-nav-label">{_e(text)}</div>'


def threads_html(threads: Sequence[Dict[str, Any]], active_thread: str = "") -> str:
    rows = []
    for thread in threads:
        thread_id = str(thread.get("thread_id", ""))
        title = thread.get("title") or "未命名会话"
        meta = thread.get("updated_at") or thread.get("last_at") or ""
        cls = "sl-thread sl-thread--active" if thread_id and thread_id == active_thread else "sl-thread"
        rows.append(
            f'<div class="{cls}"><div class="sl-thread-title">{_e(title)}</div>'
            f'<div class="sl-thread-meta">{_e(str(meta)[:16])}</div></div>'
        )
    return "".join(rows)


def user_html(user_id: str, role: str = "") -> str:
    initials = (user_id[:2] or "U").upper()
    return (
        f'<div class="sl-user"><div class="sl-avatar">{_e(initials)}</div>'
        f'<div><div class="sl-user-name">{_e(user_id)}</div>'
        f'<div class="sl-user-role">{_e(role or "local actor")}</div></div></div>'
    )


def steps_html(steps: Sequence[Dict[str, Any]]) -> str:
    rows = []
    for step in steps:
        status = str(step.get("status", ""))
        cls = {"completed": "sl-step--done", "active": "sl-step--active", "cut": "sl-step--cut"}.get(status, "")
        ms = step.get("ms")
        ms_html = f'<span class="sl-step-ms">{_e(ms)}</span>' if ms else '<span class="sl-step-ms">—</span>'
        rows.append(
            f'<div class="sl-step {cls}"><span class="sl-step-dot"></span>'
            f'<div><div class="sl-step-name">{_e(step.get("name", ""))}</div>'
            f'<div class="sl-step-desc">{_e(step.get("desc", ""))}</div></div>{ms_html}</div>'
        )
    return f'<div class="sl-steps">{"".join(rows)}</div>'


def stats_html(items: Sequence[tuple[str, Any]]) -> str:
    cells = "".join(f'<div class="sl-stat"><span>{_e(k)}</span><b>{_e(v)}</b></div>' for k, v in items)
    return f'<div class="sl-stats">{cells}</div>'


def rail_block(title: str, body: str, *, extra: str = "") -> str:
    extra_html = f"<em>{_e(extra)}</em>" if extra else ""
    return f'<div class="sl-rail-block"><div class="sl-rail-title">{_e(title)}{extra_html}</div>{body}</div>'


# ---------- 行程装备结果（plan） ----------
def _constraint_rows(constraints: Sequence[Dict[str, Any]]) -> str:
    rows = [
        '<div class="sl-row sl-row--head"><span>内容</span>'
        '<span class="sl-row-src">来源</span><span class="sl-row-hard">硬度</span><span>状态</span></div>'
    ]
    for item in constraints:
        hard = item.get("hard", True)
        status = str(item.get("status", "unverified"))
        status_tone = {"ok": "ok", "violated": "danger", "relaxed": "warn"}.get(status, "muted")
        rows.append(
            '<div class="sl-row">'
            f'<span class="sl-row-label">{_e(item.get("text", ""))}</span>'
            f'<span class="sl-row-src">{_e(item.get("source", ""))}</span>'
            f'<span class="sl-row-hard">{tag("硬" if hard else "软", "hard" if hard else "soft")}</span>'
            f'<span>{tag(STATUS_LABEL.get(status, status), status_tone)}</span>'
            "</div>"
        )
    return f'<div class="sl-rows">{"".join(rows)}</div>'


def _day_html(day: Dict[str, Any]) -> str:
    items = []
    subtotal = 0.0
    for entry in day.get("items", []) or []:
        try:
            subtotal += float(entry.get("cost") or 0)
        except (TypeError, ValueError):
            pass
        duration = entry.get("duration_min")
        detail = f"{duration} min" if duration else ""
        cost = entry.get("cost")
        items.append(
            '<div class="sl-tl-item">'
            f'<span class="sl-tl-time">{_e(entry.get("time", ""))}</span>'
            f'<span class="sl-tl-act">{_e(entry.get("activity", ""))}'
            + (f"<small>{_e(detail)}</small>" if detail else "")
            + "</span>"
            f'<span class="sl-tl-cost">{_money(cost) if cost else "—"}</span></div>'
        )
    if not items:
        items.append('<div class="sl-tl-item"><span class="sl-tl-time">—</span><span class="sl-tl-act">暂无安排</span><span class="sl-tl-cost">—</span></div>')
    if not day.get("title"):
        first = (day.get("items") or [{}])[0].get("activity", "")
        day = dict(day, title=" · ".join(x for x in (day.get("weather", ""), first) if x) or "行程")
    return (
        '<div class="sl-day">'
        f'<div class="sl-day-head"><span class="sl-day-no">D{_e(day.get("day", ""))}</span>'
        f'<span class="sl-day-name">{_e(day.get("title", "") or "行程")}</span>'
        f'<span class="sl-day-sum">{_money(subtotal)}</span></div>'
        f'<div class="sl-tl">{"".join(items)}</div></div>'
    )


def _gear_columns(gear: Sequence[Dict[str, Any]], products: Dict[str, Any]) -> tuple[str, str]:
    groups: Dict[str, List[Dict[str, Any]]] = {"owned": [], "need_buy": [], "self_provided": [], "skipped": []}
    for item in gear:
        groups.setdefault(str(item.get("status", "need_buy")), []).append(item)

    columns = []
    for key in ("owned", "need_buy", "self_provided"):
        items = groups.get(key) or []
        rows = []
        for item in items:
            product = products.get(item.get("name")) or {}
            price_html = "—"
            if key == "need_buy":
                if product.get("price"):
                    price_html = f'{_money(product.get("price"))}<small>{_e(product.get("brand", "") or "自选")}</small>'
                else:
                    price_html = '未匹配<small>未计入</small>'
            reason = item.get("reason") or ""
            qty = item.get("qty") or 1
            rows.append(
                '<div class="sl-gear-row">'
                f'<span class="sl-gear-name">{_e(item.get("name", ""))}<span class="qty">×{_e(qty)}</span></span>'
                f'<span class="sl-gear-price">{price_html}</span>'
                f'<span class="sl-gear-reason">{_e(reason)}</span></div>'
            )
        if not rows:
            rows.append('<div class="sl-gear-row"><span class="sl-gear-name">—</span><span class="sl-gear-price">—</span></div>')
        columns.append(
            f'<div class="sl-gear-col"><div class="sl-gear-head">{GEAR_STATUS_LABEL[key]}'
            f'<span class="n">{len(items)}</span></div>{"".join(rows)}</div>'
        )

    foot = ""
    skipped = groups.get("skipped") or []
    if skipped:
        names = "、".join(str(item.get("name", "")) for item in skipped)
        foot = f'<div class="sl-gear-foot">已按预算省略：{_e(names)}</div>'
    return f'<div class="sl-gear">{"".join(columns)}</div>', foot


def plan_panels_html(payload: Dict[str, Any]) -> str:
    """把 trip_gear 的结构化结果渲染成 01–05 面板。"""
    facts = payload.get("facts") or {}
    constraints = payload.get("constraints") or []
    itinerary = payload.get("itinerary") or {}
    gear = payload.get("gear") or []
    products = payload.get("products") or {}
    issues = payload.get("issues") or []
    ambiguous = payload.get("owned_ambiguous") or []

    days = itinerary.get("days") or []
    trip_total = 0.0
    for day in days:
        for entry in day.get("items", []) or []:
            try:
                trip_total += float(entry.get("cost") or 0)
            except (TypeError, ValueError):
                pass

    gear_total = 0.0
    missing = 0
    for item in gear:
        if item.get("status") != "need_buy":
            continue
        product = products.get(item.get("name")) or {}
        if product.get("price"):
            try:
                gear_total += float(product["price"]) * float(item.get("qty") or 1)
            except (TypeError, ValueError):
                missing += 1
        else:
            missing += 1
    total = trip_total + gear_total
    budget = facts.get("budget")
    has_budget = isinstance(budget, (int, float))
    remaining = (float(budget) - total) if has_budget else None
    pct = 0.0
    if has_budget and budget:
        pct = max(0.0, min(100.0, total / float(budget) * 100))

    parts = [
        panel_html(
            "01",
            "约束清单",
            f"{len(constraints)} 条 · 硬 {sum(1 for c in constraints if c.get('hard', True))} / 软 {sum(1 for c in constraints if not c.get('hard', True))}",
            _constraint_rows(constraints),
            flush=True,
        )
    ]

    if days:
        day_html = "".join(_day_html(day) for day in days)
        parts.append(
            panel_html(
                "02",
                "逐日行程",
                f"{len(days)} 天 · 行程合计 {_money(trip_total)}",
                f'<div class="sl-days">{day_html}</div>',
            )
        )

    if gear:
        columns, foot = _gear_columns(gear, products)
        owned = sum(1 for item in gear if item.get("status") == "owned")
        need = sum(1 for item in gear if item.get("status") == "need_buy")
        provided = sum(1 for item in gear if item.get("status") == "self_provided")
        parts.append(
            panel_html(
                "03",
                "装备清单",
                f"{len(gear)} 项 · 已有 {owned} / 待购 {need} / 自备 {provided}",
                f'{columns}{foot}',
            )
        )

    over = remaining is not None and remaining < 0
    delta_class = "sl-delta sl-delta--over" if over else "sl-delta"
    if remaining is None:
        delta_value = "未设预算"
    else:
        delta_value = f"{'+' if remaining >= 0 else '−'}{_money(abs(remaining))}"
    bar_class = "sl-bar-fill sl-bar-fill--over" if over else "sl-bar-fill"
    scope = "含装备" if (facts.get("budget_scope") or "trip_and_gear") != "trip_only" else "不含装备"
    side_rows = [("行程", _money(trip_total)), ("装备", _money(gear_total))]
    if missing:
        side_rows.append(("未匹配", f"{missing} 项"))
    side_rows.append(("预算口径", scope))
    kv = "".join(f'<div class="sl-kv-row"><span>{_e(k)}</span><b>{_e(v)}</b></div>' for k, v in side_rows)
    parts.append(
        panel_html(
            "04",
            "预算占用",
            f"口径：{scope}" + (f" · 预算 {_money(budget)}" if has_budget else ""),
            f"""
            <div class="sl-budget">
              <div>
                <div class="sl-budget-nums">
                  <div class="sl-budget-num"><span>行程</span><b>{_money(trip_total)}</b></div>
                  <div class="sl-budget-num"><span>装备</span><b>{_money(gear_total)}</b></div>
                  <div class="sl-budget-num"><span>合计</span><b>{_money(total)}</b></div>
                  <div class="sl-budget-num"><span>预算</span><b>{_money(budget) if has_budget else "未设"}</b></div>
                  <div class="{delta_class}"><span>剩余</span><b>{_e(delta_value)}</b></div>
                </div>
                <div class="sl-bar"><div class="{bar_class}" style="width:{pct:.1f}%"></div></div>
                <div class="sl-bar-legend"><span>已占用 {pct:.0f}%</span>
                  {'' if not over else f'<span>超支 {_money(abs(remaining))}</span>'}</div>
              </div>
              <div class="sl-budget-side"><div class="sl-kv">{kv}</div></div>
            </div>
            """,
        )
    )

    # 商品库未覆盖的装备不是约束冲突，单独并成一条提示，避免刷屏。
    unmatched = [item for item in issues if "未匹配" in str(item.get("hint") or "")]
    violations = [item for item in issues if item not in unmatched]

    alerts = []
    if not violations:
        alerts.append(alert_html("ok", "未发现硬约束冲突", "预算、时间重叠、天数一致性与必备装备均已通过确定性校验。"))
    else:
        for issue in violations[:4]:
            detail = issue.get("hint") or ""
            if issue.get("actual") is not None and issue.get("limit") is not None:
                detail = f"实际 {issue.get('actual')}，上限 {issue.get('limit')}。{detail}"
            alerts.append(alert_html("warn", issue.get("constraint", "未满足约束"), detail))
    if unmatched:
        names = "、".join(str(item.get("constraint", "")).replace("装备：", "") for item in unmatched[:6])
        more = f" 等 {len(unmatched)} 项" if len(unmatched) > 6 else ""
        alerts.append(alert_html(
            "",
            f"{len(unmatched)} 项装备商品库未覆盖",
            f"{names}{more}；未编造价格，也未计入预算。",
        ))
    for phrase in ambiguous[:3]:
        alerts.append(alert_html("warn", "待确认", f"你提到过「{phrase}」，但没说具体物品，暂未算作已有。"))
    if alerts:
        parts.append(panel_html("05", "校验结果", "确定性规则 · 不依赖模型自审", "".join(alerts)))
    return "".join(parts)
