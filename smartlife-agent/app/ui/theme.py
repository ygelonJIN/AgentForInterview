"""全局 UI 视觉规范与共用展示组件。"""
from __future__ import annotations

from html import escape
from typing import Iterable, Optional, Sequence

import streamlit as st


COLORS = {
    "ink": "#172033",
    "muted": "#667085",
    "surface": "#ffffff",
    "canvas": "#f4f6fa",
    "line": "#e5e9f2",
    "primary": "#4f46e5",
    "primary_dark": "#3730a3",
    "teal": "#0f766e",
    "amber": "#d97706",
    "danger": "#dc2626",
}

NAV_ITEMS = (
    ("✦", "智能助手", "assistant"),
    ("◫", "生活目录", "catalog"),
    ("◇", "记忆中心", "memory"),
    ("⇧", "数据导入", "import"),
    ("○", "个人中心", "profile"),
)

THEME_CSS = """
<style>
:root {
  --top-offset: 1.75rem;
  --control-radius: 14px;
  --ink: #172033;
  --muted: #667085;
  --surface: #ffffff;
  --canvas: #f4f6fa;
  --line: #e5e9f2;
  --primary: #4f46e5;
  --primary-soft: #eef2ff;
  --teal: #0f766e;
  --teal-soft: #ecfdf5;
  --amber: #d97706;
  --amber-soft: #fff7ed;
  --danger: #dc2626;
  --shadow: 0 12px 30px rgba(31, 42, 68, 0.08);
}

html, body, [class*="css"], .stApp {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", "Microsoft YaHei", sans-serif;
  color: var(--ink);
}
[data-testid="stMainBlockContainer"] {
  max-width: 1280px;
  padding-top: var(--top-offset) !important;
  padding-bottom: 9.5rem !important;
}
.stApp {
  background:
    radial-gradient(circle at 100% 0%, rgba(79, 70, 229, 0.08), transparent 28rem),
    linear-gradient(180deg, #f8f9fc 0%, var(--canvas) 100%);
}
#MainMenu, footer { visibility: hidden !important; }
[data-testid="stMainMenuButton"],
[data-testid="stBaseButton-header"] { visibility: hidden !important; }
[data-testid="stHeader"] {
  height: 0 !important;
  min-height: 0 !important;
  padding: 0 !important;
  border: 0 !important;
  background: transparent !important;
}
[data-testid="stSidebarHeader"] {
  height: 0 !important;
  min-height: 0 !important;
  padding: 0 !important;
  margin: 0 !important;
  overflow: visible !important;
}
[data-testid="stSidebarCollapseButton"],
[data-testid="stExpandSidebarButton"] {
  top: 11px !important;
  display: block !important;
  visibility: visible !important;
  opacity: 1 !important;
  z-index: 1300;
}
[data-testid="stSidebarCollapseButton"] {
  position: absolute !important;
  right: 13px !important;
  left: auto !important;
}
[data-testid="stExpandSidebarButton"] {
  position: fixed !important;
  left: 13px !important;
  right: auto !important;
}
[data-testid="stSidebarCollapseButton"] button,
[data-testid="stExpandSidebarButton"] button {
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
  width: 30px !important;
  height: 30px !important;
  padding: 0 !important;
  border: 0 !important;
  border-radius: 8px !important;
  background: transparent !important;
  box-shadow: none !important;
  visibility: visible !important;
  opacity: 1 !important;
}
[data-testid="stSidebarCollapseButton"] button:hover,
[data-testid="stExpandSidebarButton"] button:hover {
  background: rgba(79, 70, 229, 0.08) !important;
}
[data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"],
[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] {
  color: var(--ink) !important;
  visibility: visible !important;
  opacity: 1 !important;
}
section[data-testid="stSidebar"] {
  background: linear-gradient(180deg, #312e81 0%, #4f46e5 100%);
  border-right: 1px solid #4338ca;
}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
  padding-top: var(--top-offset) !important;
}
.sidebar-brand {
  padding: 0 2.85rem 1.15rem 0.25rem;
}
.sidebar-kicker {
  color: #c7d2fe;
  font-size: 0.72rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  font-weight: 800;
}
.sidebar-title {
  margin-top: 0.32rem;
  color: #ffffff;
  font-size: 1.55rem;
  line-height: 1.22;
  font-weight: 800;
}
.sidebar-copy {
  margin-top: 0.48rem;
  color: #c7d2fe;
  font-size: 0.88rem;
  line-height: 1.65;
}
section[data-testid="stSidebar"] .stButton > button {
  width: 100%;
  min-height: 52px;
  justify-content: flex-start;
  text-align: left;
  padding: 0.78rem 0.95rem !important;
  border: 1px solid var(--line) !important;
  border-radius: var(--control-radius) !important;
  background: var(--surface) !important;
  color: var(--ink) !important;
  font-weight: 650;
  box-shadow: 0 3px 10px rgba(31, 42, 68, 0.04) !important;
  transition: 160ms ease !important;
}
section[data-testid="stSidebar"] .stButton > button:hover {
  border-color: #c7d2fe !important;
  background: #ffffff !important;
  color: var(--primary-dark) !important;
  box-shadow: 0 8px 18px rgba(109, 40, 217, 0.14) !important;
  transform: translateY(-1px) !important;
}
section[data-testid="stSidebar"] .stButton > button[kind="primary"],
section[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
  color: #ffffff !important;
  border-color: #818cf8 !important;
  background: #6366f1 !important;
  box-shadow: 0 10px 22px rgba(79, 70, 229, 0.28) !important;
}
section[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
  background: #4f46e5 !important;
  border-color: #a5b4fc !important;
  transform: translateY(-1px) !important;
}
section[data-testid="stSidebar"] hr {
  border-color: rgba(255, 255, 255, 0.16);
  margin: 1.15rem 0;
}
section[data-testid="stSidebar"] .stCaption,
section[data-testid="stSidebar"] .stMarkdown p {
  color: #c7d2fe !important;
}
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] label {
  color: #dbe3ff !important;
}
h1, h2, h3, p { letter-spacing: -0.015em; }
h1 { font-weight: 800; }
h2 { font-weight: 750; }
h3 { font-weight: 700; }
.stButton > button, .stDownloadButton > button {
  border-radius: 11px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink);
  font-weight: 650;
  padding: 0.58rem 1rem;
  transition: 160ms ease;
  box-shadow: 0 3px 10px rgba(31, 42, 68, 0.04);
}
.stButton > button:hover {
  border-color: #c7d2fe;
  color: var(--primary-dark);
  transform: translateY(-1px);
}
.stButton > button {
  min-height: 46px;
}
.stButton > button[kind="primary"] {
  color: #312e81;
  border-color: #c7d2fe;
  background: #e9edff;
  box-shadow: 0 8px 18px rgba(79, 70, 229, 0.12);
}
.stTextInput input, .stTextArea textarea, .stSelectbox [data-baseweb="select"], .stNumberInput input {
  border-radius: 11px;
  border-color: var(--line);
  background: var(--surface);
}
.stTextInput input:focus, .stTextArea textarea:focus {
  border-color: #a5b4fc;
  box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.12);
}
[data-testid="stExpander"] {
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
  overflow: hidden;
}
[data-testid="stExpander"] summary { font-weight: 700; }
[data-testid="stChatMessage"] {
  background: transparent;
  padding: 0.25rem 0;
}
[data-testid="stChatMessage"] [data-testid="ChatAvatar"] {
  background: linear-gradient(135deg, var(--primary), #7c3aed);
}
[data-testid="stChatMessage"]:has([data-testid="ChatAvatar"]) {
  border-bottom: 0;
}
[data-testid="stBottom"] {
  position: fixed !important;
  left: 0 !important;
  right: 0 !important;
  bottom: 0 !important;
  z-index: 1100 !important;
  padding: 1rem 4rem 0.72rem calc(300px + 4rem) !important;
  background: transparent !important;
  pointer-events: none;
}
:where([data-testid="stBottomBlockContainer"]) :where(.stElementContainer, .stVerticalBlock, .stVerticalBlockBorderWrapper, [data-testid="stChatInput"]),
:where([data-testid="stBottomBlockContainer"]) :where([data-testid="stChatInput"]) * {
  margin: 0 !important;
  border: 0 !important;
  outline: 0 !important;
  border-radius: 0 !important;
  background: transparent !important;
  background-image: none !important;
  box-shadow: none !important;
  filter: none !important;
  text-shadow: none !important;
}
:where([data-testid="stBottomBlockContainer"]) *::before,
:where([data-testid="stBottomBlockContainer"]) *::after {
  content: none !important;
  display: none !important;
}
[data-testid="stBottom"] > div,
[data-testid="stBottom"] [class*="e8oj7lc3"],
[data-testid="stBottom"] > div:has(> [data-testid="stBottomBlockContainer"]) {
  border: 0 !important;
  border-radius: 0 !important;
  background: transparent !important;
  background-color: transparent !important;
  background-image: none !important;
  box-shadow: none !important;
}
[data-testid="stBottomBlockContainer"] {
  max-width: 1280px;
  margin: 0 auto !important;
  padding: 0 !important;
  background: transparent !important;
  pointer-events: auto;
}
[data-testid="stAppViewContainer"]:has(
  section[data-testid="stSidebar"][aria-expanded="false"]
) [data-testid="stBottom"] {
  padding-left: 4rem !important;
}
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] {
  position: relative !important;
  display: flex !important;
  align-items: center !important;
  min-height: 58px !important;
  padding: 7px 8px 7px 15px !important;
  border: 0 !important;
  outline: 0 !important;
  border-radius: var(--control-radius) !important;
  background: #ffffff !important;
  box-shadow: 0 18px 60px rgba(31, 42, 68, 0.08) !important;
  isolation: isolate;
}
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"]:focus-within {
  box-shadow: 0 18px 60px rgba(31, 42, 68, 0.08), 0 0 0 3px rgba(79, 70, 229, 0.10) !important;
}
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] > div {
  width: 100% !important;
  min-height: 0 !important;
  padding: 0 !important;
  border: 0 !important;
  outline: 0 !important;
  border-radius: 0 !important;
  background: transparent !important;
  box-shadow: none !important;
}
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] > div > div,
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] > div > div:focus-within,
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] textarea,
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] textarea:focus,
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] textarea:focus-visible,
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] button,
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] button:focus,
html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] button:focus-visible {
  border: 0 !important;
  outline: 0 !important;
  border-radius: 0 !important;
  background: transparent !important;
  background-image: none !important;
  box-shadow: none !important;
  filter: none !important;
  text-shadow: none !important;
}
[data-testid="stAlert"] {
  border-radius: 14px;
  border: 1px solid var(--line);
}
section[data-testid="stSidebar"] [data-testid="stAlert"] {
  border: 1px solid rgba(199, 210, 254, 0.38) !important;
  border-radius: var(--control-radius) !important;
  background: rgba(30, 27, 75, 0.28) !important;
  box-shadow: 0 8px 20px rgba(15, 23, 42, 0.12) !important;
}
section[data-testid="stSidebar"] [data-testid="stAlert"] *,
section[data-testid="stSidebar"] [data-testid="stAlert"] p {
  color: #ffffff !important;
}
section[data-testid="stSidebar"] [data-testid="stAlert"] svg {
  color: #ffffff !important;
  fill: #ffffff !important;
}
[data-testid="stVerticalBlock"] > [data-testid="stVerticalBlockBorderWrapper"] {
  border-color: var(--line);
  border-radius: 18px;
}

.app-hero {
  position: relative;
  overflow: hidden;
  padding: 2rem 2.15rem;
  border-radius: 24px;
  color: white;
  background:
    radial-gradient(circle at 85% 25%, rgba(255,255,255,0.18), transparent 18rem),
    linear-gradient(135deg, #312e81 0%, #4f46e5 52%, #6d5dfc 100%);
  box-shadow: 0 22px 55px rgba(79, 70, 229, 0.22);
  margin-bottom: 1.45rem;
}
.app-hero .eyebrow {
  font-size: 0.76rem;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  opacity: 0.78;
  font-weight: 700;
}
.app-hero h1 {
  color: white;
  font-size: clamp(2rem, 4vw, 3.15rem);
  line-height: 1.05;
  margin: 0.32rem 0 0.65rem;
}
.app-hero p {
  color: rgba(255,255,255,0.82);
  max-width: 760px;
  font-size: 1.02rem;
  line-height: 1.75;
  margin: 0;
}

.page-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 1.5rem;
  margin: 0.25rem 0 1.35rem;
}
.page-heading h1 {
  margin: 0 0 0.35rem;
  font-size: 2.15rem;
  line-height: 1.15;
}
.page-heading p { margin: 0; color: var(--muted); line-height: 1.7; }
.page-heading .pill {
  display: inline-flex;
  border-radius: 999px;
  padding: 0.42rem 0.82rem;
  background: var(--primary-soft);
  color: var(--primary-dark);
  font-size: 0.78rem;
  font-weight: 750;
}
.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 1.75rem 0 0.95rem;
}
.section-heading h2 { margin: 0; font-size: 1.25rem; }
.section-heading p { margin: 0.25rem 0 0; color: var(--muted); }

.ui-card {
  height: 100%;
  padding: 1.15rem 1.18rem 1.12rem;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: rgba(255,255,255,0.92);
  box-shadow: var(--shadow);
}
.ui-card.directory-card {
  height: 224px;
  min-height: 224px;
  max-height: 224px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.ui-card.directory-card h3 {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}
.ui-card.directory-card p {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
  flex: 1 1 auto;
}
.ui-card.directory-card .card-meta {
  margin-top: auto;
  flex-wrap: nowrap;
  overflow: hidden;
}
.ui-card:hover {
  border-color: #c7d2fe;
  transform: translateY(-2px);
}
.ui-card .card-kicker {
  color: var(--primary);
  font-size: 0.74rem;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  font-weight: 800;
}
.ui-card h3 { margin: 0.55rem 0 0.55rem; font-size: 1.08rem; line-height: 1.45; }
.ui-card p { color: var(--muted); line-height: 1.72; margin: 0.25rem 0; }
.ui-card .card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  margin-top: 0.85rem;
}
.ui-card .pill {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 0.28rem 0.62rem;
  background: var(--primary-soft);
  color: var(--primary-dark);
  font-size: 0.76rem;
  font-weight: 700;
}
.ui-card .pill.teal { background: var(--teal-soft); color: var(--teal); }
.ui-card .pill.amber { background: var(--amber-soft); color: var(--amber); }

.metric-card {
  padding: 1.15rem 1.2rem;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: rgba(255,255,255,0.92);
  box-shadow: 0 10px 24px rgba(31, 42, 68, 0.06);
}
.metric-card .metric-label {
  color: var(--muted);
  font-size: 0.82rem;
  font-weight: 650;
}
.metric-card .metric-value {
  margin-top: 0.35rem;
  font-size: 1.65rem;
  line-height: 1.15;
  font-weight: 800;
}
.metric-card .metric-note {
  margin-top: 0.45rem;
  color: var(--muted);
  font-size: 0.78rem;
}

.process-card {
  padding: 0.82rem 0.95rem;
  margin: 0.55rem 0;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: #fbfcff;
}
.process-card.active { border-color: #c7d2fe; background: var(--primary-soft); }
.process-card.completed { border-color: #a7f3d0; background: var(--teal-soft); }
.process-card.waiting { border-color: #fed7aa; background: var(--amber-soft); }
.process-card.error { border-color: #fecaca; background: #fff1f2; }
.process-card .process-title { font-weight: 750; }
.process-card .process-copy { color: var(--muted); margin-top: 0.18rem; line-height: 1.55; }
.execution-log-panel {
  margin-top: 0.75rem;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.78);
  overflow: hidden;
}
.execution-log-panel > summary {
  cursor: pointer;
  padding: 0.72rem 0.9rem;
  color: var(--ink);
  font-weight: 720;
}
.execution-log-list { padding: 0 0.9rem 0.75rem; }
.execution-log-row {
  display: grid;
  grid-template-columns: 1.35rem minmax(0, 1fr);
  gap: 0.55rem;
  padding: 0.55rem 0;
  border-top: 1px solid var(--line);
}
.execution-log-icon {
  width: 1.25rem;
  height: 1.25rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 850;
  background: var(--primary-soft);
  color: var(--primary-dark);
}
.execution-log-ok .execution-log-icon { background: var(--teal-soft); color: var(--teal); }
.execution-log-warning .execution-log-icon { background: var(--amber-soft); color: var(--amber); }
.execution-log-error .execution-log-icon { background: #fff1f2; color: var(--danger); }
.execution-log-meta {
  color: var(--muted);
  font-size: 0.76rem;
  line-height: 1.5;
}
.execution-log-message {
  margin-top: 0.12rem;
  color: var(--ink);
  font-size: 0.86rem;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.review-card {
  padding: 1rem 1.1rem;
  border: 1px solid var(--line);
  border-radius: 15px;
  background: white;
  margin-bottom: 0.75rem;
}
.review-card .review-head {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  font-weight: 700;
}
.review-card .review-copy { color: var(--muted); line-height: 1.72; margin-top: 0.55rem; }

.form-action-spacer {
  height: 1.72rem;
}

.mini-empty {
  padding: 1.35rem;
  border: 1px dashed #cbd5e1;
  border-radius: 16px;
  color: var(--muted);
  text-align: center;
  background: rgba(255,255,255,0.58);
}

@media (max-width: 760px) {
  [data-testid="stBottom"] {
    padding-left: 1.15rem !important;
    padding-right: 1.15rem !important;
    padding-bottom: 0.65rem !important;
    background: transparent !important;
  }
  [data-testid="stMainBlockContainer"] {
    padding-left: 1.15rem;
    padding-right: 1.15rem;
    padding-bottom: 8.75rem;
  }
  .app-hero { padding: 1.55rem 1.35rem; }
  .page-heading { align-items: flex-start; flex-direction: column; }
}
</style>
"""


def inject_theme() -> None:
    st.markdown(THEME_CSS, unsafe_allow_html=True)


def render_hero(eyebrow: str, title: str, description: str) -> None:
    st.markdown(
        f"""
        <div class="app-hero">
          <div class="eyebrow">{escape(eyebrow)}</div>
          <h1>{escape(title)}</h1>
          <p>{escape(description)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_page_header(title: str, description: str, meta: str = "") -> None:
    meta_html = f'<div class="pill">{escape(meta)}</div>' if meta else ""
    st.markdown(
        f"""
        <div class="page-heading">
          <div><h1>{escape(title)}</h1><p>{escape(description)}</p></div>
          <div>{meta_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section_header(title: str, description: str = "") -> None:
    copy = f"<p>{escape(description)}</p>" if description else ""
    st.markdown(
        f'<div class="section-heading"><div><h2>{escape(title)}</h2>{copy}</div></div>',
        unsafe_allow_html=True,
    )


def render_metric(label: str, value: object, note: str = "") -> None:
    note_html = f'<div class="metric-note">{escape(note)}</div>' if note else ""
    st.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-label">{escape(label)}</div>
          <div class="metric-value">{escape(str(value))}</div>
          {note_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_card(
    title: str,
    *,
    kicker: str = "",
    description: str = "",
    meta: Iterable[str] = (),
    tone: str = "",
) -> None:
    kicker_html = f'<div class="card-kicker">{escape(kicker)}</div>' if kicker else ""
    meta_items = "".join(f'<span class="pill {escape(tone)}">{escape(item)}</span>' for item in meta)
    st.markdown(
        f"""
        <div class="ui-card directory-card">
          {kicker_html}
          <h3>{escape(title)}</h3>
          <p>{escape(description)}</p>
          <div class="card-meta">{meta_items}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_review(
    author: str,
    rating: int | float,
    content: str,
    product_name: Optional[str] = None,
) -> None:
    title = product_name or author
    right = f"{rating}/5 · {author}" if product_name else f"{rating}/5"
    st.markdown(
        f"""
        <div class="review-card">
          <div class="review-head"><span>{escape(title)}</span><span>{escape(right)}</span></div>
          <div class="review-copy">{escape(content)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_process_card(
    name: str,
    description: str = "",
    status: str = "pending",
    step: object = "",
) -> None:
    marker = f"{step}. " if step != "" else ""
    copy = f'<div class="process-copy">{escape(description)}</div>' if description else ""
    st.markdown(
        f"""
        <div class="process-card {escape(status)}">
          <div class="process-title">{escape(marker + name)}</div>
          {copy}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_empty(message: str) -> None:
    st.markdown(f'<div class="mini-empty">{escape(message)}</div>', unsafe_allow_html=True)


def metric_columns(items: Sequence[tuple[str, object, str]], columns: int = 4):
    cols = st.columns(columns)
    for col, (label, value, note) in zip(cols, items):
        with col:
            render_metric(label, value, note)
    return cols
