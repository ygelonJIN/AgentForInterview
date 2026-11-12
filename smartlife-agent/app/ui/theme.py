"""全局视觉规范与共用展示组件（V2 · 暖纸手账）。

改版说明（2026-11）：
- 视觉全部来自 ``app/ui/tokens.py`` 的 token，本文件不写死颜色/圆角/字号。
- 结构对齐 ``frontend-preview/template.html``：左导航、主列 + 右轨、编号面板。
- Streamlit 原生控件（按钮/输入/聊天/展开器/标签页）统一在本文件重绘，
  页面层只负责编排，不再各自写样式。

组件命名约定：``render_*`` 输出 HTML；``*_html`` 只拼字符串（可复用于流式渲染）。
"""
from __future__ import annotations

from datetime import date
from html import escape
from typing import Any, Dict, Iterable, List, Optional, Sequence

import streamlit as st

from app.ui.tokens import css_variables

# 导航：图标、文案、页面 key。与前端原型保持一致。
# 导航只有文字，没有图标：图标只能靠 nth-child 对位，加一项就会串位。
NAV_ITEMS = (
    ("智能助手", "assistant"),
    ("生活目录", "catalog"),
    ("记忆中心", "memory"),
    ("数据导入", "import"),
    ("Agent 配置", "agents"),
    ("个人中心", "profile"),
)

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
/* 运行状态指示器（Run/Stop 那颗跑步小人）：它按 header 高度垂直居中，
   头部归零后整颗图标有一半跑到容器顶边外面被裁掉。脱离头部自己钉在右上角。 */
[data-testid="stStatusWidget"] {{
  position: fixed !important;
  top: 10px !important;
  right: 14px !important;
  z-index: 140;
}}

/* ---------- 页面骨架 ---------- */
/* 只有右栏（stMain）滚动：body 不滚，滚轮在对话区也只滚对话区 */
html, body, #root {{ height: 100%; overflow: hidden; }}
.stApp {{ height: 100vh; overflow: hidden; }}
[data-testid="stAppViewContainer"] {{ background: transparent; height: 100vh; overflow: hidden; }}
[data-testid="stMain"] {{ min-height: 0; overflow-y: auto; overscroll-behavior: contain; }}
[data-testid="stMainBlockContainer"] {{
  /* 侧栏收起时对话区要铺满：不再固定 1180，只靠左右留白控制行长 */
  max-width: 100%;
  /* 页头已回到文档流；顶部留足空白，第一条消息不会贴着窗口顶 */
  padding: var(--space-6) var(--space-6) 9.5rem;
}}
[data-testid="stMainBlockContainer"] > div {{ gap: var(--space-4); }}
[data-testid="stVerticalBlock"] {{ gap: var(--space-3); }}
[data-testid="stHorizontalBlock"] {{ gap: var(--space-4); }}

/* ---------- 左导航 ---------- */
section[data-testid="stSidebar"] {{
  background: var(--c-nav-bg) !important;
  border-right: var(--border-w) solid var(--c-line);
}}
/* 只在展开时钉宽度：收起时若继续 !important 钉住，主区永远补不上来 */
section[data-testid="stSidebar"][aria-expanded="true"] {{
  width: var(--nav-w) !important;
  min-width: var(--nav-w) !important;
}}
section[data-testid="stSidebar"][aria-expanded="false"] {{
  width: 0 !important;
  min-width: 0 !important;
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
/* 侧栏按钮一律用后代选择器：带 help 的按钮会被 tooltip 的 span 包一层，
   写成直接子选择器就匹配不到，样式会整段落空（新建对话、每一条会话都带 help）。 */
section[data-testid="stSidebar"] .stButton button {{
  width: 100%;
  min-height: 38px;
  justify-content: flex-start;
  text-align: left;
  /* 按钮是"控件"不是"文字行"：左对齐看的是按钮框（36.1），框内仍留 13px，
     不然「＋新建对话」的加号会贴着自己的左边框。纯文字（品牌/分区标题/
     会话行）另走 2px 竖线槽，两条边界各管各的。 */
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
section[data-testid="stSidebar"] .stButton button:hover {{
  background: var(--c-nav-hover-bg) !important;
  color: var(--c-nav-active-text) !important;
}}
section[data-testid="stSidebar"] .stButton button > div {{ width: 100%; justify-content: flex-start !important; gap: var(--space-2) !important; }}
section[data-testid="stSidebar"] .stButton button p {{ white-space: nowrap; }}
section[data-testid="stSidebar"] .stButton button[kind="primary"],
section[data-testid="stSidebar"] .stButton button[kind="primary"]:hover {{
  background: var(--c-nav-active-bg) !important;
  border-color: var(--c-nav-active-line) !important;
  color: var(--c-nav-active-text) !important;
  font-weight: var(--fw-semibold) !important;
  box-shadow: var(--shadow-1) !important;
}}
section[data-testid="stSidebar"] [data-testid="stAlert"] {{ border-radius: var(--radius-sm); }}

/* 「新建对话」用 type="primary"，跟工作台选中项共用同一条规则，这里只留间距。 */
.st-key-new_thread {{ margin: var(--space-1) 0 var(--space-2); }}
section[data-testid="stSidebar"] .st-key-new_thread .stButton button p {{ white-space: nowrap; }}
/* 导航只有文字：不再有图标占位，标签统一左对齐。 */
.st-key-nav_menu .stButton button {{ justify-content: flex-start; padding-left: var(--space-3); }}


/* 左导轨：侧栏里所有文字（品牌、分区标题、导航、会话行）共用同一条左边界。
   会话行用 2px 透明左边框给"当前"竖线留槽，所以文字从 2px 处起步，
   这里统一补同一个 2px，整列才不会一处一个缩进。 */
.sl-brand {{ display: flex; align-items: center; gap: var(--space-3); padding: 0 var(--space-1) var(--space-4) 2px; }}
.sl-brand-name {{ font-family: var(--font-display); font-size: var(--fs-md); font-weight: var(--fw-bold); color: var(--c-nav-text); }}
.sl-brand-sub {{
  font-size: var(--fs-2xs); color: var(--c-nav-muted);
  letter-spacing: var(--tracking-caps); text-transform: uppercase;
}}
.sl-nav-label {{
  padding: var(--space-3) 2px var(--space-1) 2px;
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

/* 最近会话：一行一条，用左侧短竖线做"当前"标记。
   刻意不跟导航的选中态同款（导航是整块强调底色 + 图标），
   这里只有一条竖线 + 文字加深，安静一档。 */
/* 侧栏不再整体滚动：只有会话列表内部滚动。
   滚轮停在侧栏其它位置时不带动整页：让侧栏自身永远有 2px 可滚空间 +
   overscroll-behavior: contain，事件在这里被吃掉，不会链到页面。 */
section[data-testid="stSidebar"] {{
  overflow-y: scroll !important;
  overscroll-behavior: contain;
  scrollbar-width: none;
}}
section[data-testid="stSidebar"]::-webkit-scrollbar {{ width: 0; height: 0; }}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {{ overflow: hidden !important; }}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{ overflow: hidden !important; height: 100%; }}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div {{ height: 100%; min-height: 0; overflow: hidden; }}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] {{
  height: calc(100% + 2px); min-height: 0; overflow: hidden;
  display: flex; flex-direction: column; gap: var(--space-2);
}}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] {{ flex: 0 0 auto; min-height: 0; }}

.st-key-recent_threads {{
  flex: 1 1 auto; min-height: 0; max-height: 52vh;
  overflow-y: auto; overflow-x: hidden;
  overscroll-behavior: contain;
  /* 右留 14px 给滚动条：macOS 是悬浮滚动条，不占布局，会直接压在行尾的 ✕ 上 */
  padding-right: 14px;
  /* 有经典滚动条的浏览器从布局里就把这条让出来，行尾不会随滚动跳动 */
  scrollbar-gutter: stable;
}}
.st-key-recent_threads [data-testid="stVerticalBlockBorderWrapper"] {{ border: none !important; box-shadow: none !important; background: transparent !important; }}
.st-key-recent_threads > [data-testid="stVerticalBlock"] {{ gap: 0; }}
section[data-testid="stSidebar"] [class*="st-key-recent_thread_"] .stButton button {{
  justify-content: flex-start !important;
  width: 100% !important;
  max-width: 100% !important;
  min-height: 0 !important;
  height: auto !important;
  /* 选中靠整行浅底，不再靠左边一条竖线；左右留 8px 让底色和文字都收在轨道内 */
  padding: 3px 8px !important;
  font-size: var(--fs-xs) !important;
  font-weight: var(--fw-body) !important;
  color: var(--c-nav-muted) !important;
  background: transparent !important;
  border: 0 !important;
  border-radius: var(--radius-sm) !important;
  box-shadow: none !important;
}}
section[data-testid="stSidebar"] [class*="st-key-recent_thread_"] .stButton button > div {{
  justify-content: flex-start !important;
  min-width: 0 !important;
}}
section[data-testid="stSidebar"] [class*="st-key-recent_thread_"] .stButton button,
section[data-testid="stSidebar"] [class*="st-key-recent_thread_"] .stButton button > div,
section[data-testid="stSidebar"] [class*="st-key-recent_thread_"] .stButton button p {{
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  overflow-wrap: normal !important;
  word-break: keep-all !important;
  max-width: 100% !important;
}}
section[data-testid="stSidebar"] [class*="st-key-recent_thread_"] .stButton button p {{
  width: 100%; text-align: left; display: block;
}}
section[data-testid="stSidebar"] [class*="st-key-recent_thread_"] .stButton button:hover {{
  background: var(--c-nav-hover-bg) !important;
  color: var(--c-nav-text) !important;
}}
section[data-testid="stSidebar"] [class*="st-key-recent_thread_"] .stButton button[kind="primary"],
section[data-testid="stSidebar"] [class*="st-key-recent_thread_"] .stButton button[kind="primary"]:hover {{
  /* 当前会话：整行浅底 + 强调字色；不摆左侧竖条，也不套导航那种描边卡片 */
  background: var(--c-nav-hover-bg) !important;
  border: 0 !important;
  border-radius: var(--radius-sm) !important;
  color: var(--c-nav-active-text) !important;
  font-weight: var(--fw-semibold) !important;
  box-shadow: none !important;
}}
/* 会话行悬停详情：钉在这一行的右边（侧栏之外），不用原生 tooltip 以免盖住上一行 */
.sl-thread-tip {{
  position: fixed;
  left: calc(var(--nav-w) + 12px);
  top: auto; right: auto; bottom: auto;
  z-index: 150;
  max-width: 280px;
  padding: var(--space-2) var(--space-3);
  border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-sm);
  background: var(--c-surface);
  box-shadow: var(--shadow-3);
  font-size: var(--fs-xs);
  color: var(--c-text-muted);
  opacity: 0;
  pointer-events: none;
}}
/* 不做过渡：鼠标一进这一行就出现，别让人等 */
[class*="st-key-recent_row_"]:hover .sl-thread-tip,
[class*="st-key-recent_thread_"]:hover .sl-thread-tip,
[class*="st-key-recent_delete_"]:hover .sl-thread-tip {{ opacity: 1; }}
.sl-thread-tip b {{
  display: block;
  color: var(--c-text);
  font-weight: var(--fw-medium);
  margin-bottom: 2px;
  white-space: normal;
}}
.sl-thread-tip span {{ display: block; white-space: nowrap; }}

/* 会话行右侧的删除按钮。
   注意特异性：侧栏通用规则 `section[data-testid="stSidebar"] .stButton button`
   是 (0,2,2)，这里必须再加一层侧栏前缀压过去，否则它的
   `padding: 8px 12px !important` 会把 18px 的小按钮内宽挤成 0，✕ 直接看不见。 */
section[data-testid="stSidebar"] [class*="st-key-recent_row_"] [class*="st-key-recent_delete_"] button {{
  box-sizing: border-box !important;
  width: 20px !important; min-width: 20px !important; max-width: 20px !important;
  height: 20px !important; min-height: 20px !important;
  padding: 0 !important;
  margin: 0 0 0 auto !important;
  display: inline-flex !important; align-items: center !important; justify-content: center !important;
  border: var(--border-w) solid transparent !important;
  border-radius: var(--radius-sm) !important;
  background: transparent !important; box-shadow: none !important;
  color: var(--c-nav-text) !important;
  font-size: 11px !important; line-height: 1 !important;
  /* 常驻可见：删的是哪一条要一眼能找到，不能等悬停才露头 */
  opacity: 0.6;
}}
section[data-testid="stSidebar"] [class*="st-key-recent_row_"] [class*="st-key-recent_delete_"] button div {{
  width: auto !important; min-width: 0 !important;
  gap: 0 !important; justify-content: center !important;
}}
section[data-testid="stSidebar"] [class*="st-key-recent_row_"] [class*="st-key-recent_delete_"] button p {{
  white-space: nowrap !important; overflow: visible !important;
}}
[class*="st-key-recent_row_"]:hover [class*="st-key-recent_delete_"] button,
[class*="st-key-recent_row_"] [class*="st-key-recent_delete_"] button:focus-visible {{ opacity: 0.95; }}
[class*="st-key-recent_row_"] [class*="st-key-recent_delete_"] button:hover {{
  opacity: 1;
  color: var(--c-danger) !important;
  border-color: var(--c-line) !important;
}}
.st-key-thread_delete_confirm {{
  padding: var(--space-2);
  border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-sm);
  background: var(--c-surface);
  margin: 2px 0 var(--space-2);
}}
.sl-confirm-line {{
  font-size: var(--fs-2xs); color: var(--c-text);
  line-height: var(--lh-tight); margin-bottom: var(--space-2);
}}
/* 确认块里的两个按钮：侧栏通用按钮 38px 高、8/12 内边距，在这里太高太胖 */
section[data-testid="stSidebar"] .st-key-thread_delete_confirm .stButton button {{
  min-height: 24px !important;
  height: 24px !important;
  padding: 0 var(--space-2) !important;
  font-size: var(--fs-2xs) !important;
  font-weight: var(--fw-medium) !important;
  border-radius: var(--radius-xs) !important;
  box-shadow: none !important;
}}
section[data-testid="stSidebar"] .st-key-thread_delete_confirm .stButton button[kind="primary"],
section[data-testid="stSidebar"] .st-key-thread_delete_confirm .stButton button[kind="primary"]:hover {{
  background: var(--c-accent) !important;
  border-color: var(--c-accent) !important;
  color: var(--c-surface) !important;
  box-shadow: none !important;
}}
section[data-testid="stSidebar"] .st-key-thread_delete_confirm [data-testid="stHorizontalBlock"] {{ gap: var(--space-2); }}
.sl-user {{
  display: flex; align-items: center; gap: var(--space-3);
  margin-top: var(--space-4); padding-top: var(--space-4);
  border-top: var(--panel-rule-w) solid var(--c-nav-active-line);
}}
.sl-user-name {{ font-size: var(--fs-xs); font-weight: var(--fw-semibold); color: var(--c-nav-text); }}
.sl-user-role {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-nav-muted); }}

/* ---------- 页头（随主区域滚动，不再固定压在最上面） ---------- */
.sl-page-head {{
  position: static !important;  /* 旧规则里是 fixed，这里显式压回文档流 */
  display: flex; align-items: flex-end; gap: var(--space-4);
  padding: var(--space-2) 0 var(--space-3);
  border-bottom: var(--panel-rule-w) solid var(--c-line);
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
  display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-1) var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-bottom: var(--panel-rule-w) solid var(--c-line);
  background: var(--c-panel-head-bg);
}}
.sl-panel-idx {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); letter-spacing: var(--tracking-caps); }}
.sl-panel-title {{ font-family: var(--font-display); font-size: var(--fs-md); font-weight: var(--fw-bold); letter-spacing: var(--tracking-display); }}
.sl-panel-meta {{ margin-left: auto; flex: 0 0 auto; white-space: nowrap; font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); }}
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

/* ---------- 约束清单（两列短行，只留内容与来源） ---------- */
.sl-rows {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: var(--space-6); }}
.sl-row {{
  display: grid; grid-template-columns: minmax(0, 1fr) auto;
  align-items: baseline; gap: var(--space-3);
  padding: var(--space-2) var(--space-5);
  border-bottom: var(--panel-rule-w) solid var(--c-line);
  font-size: var(--fs-sm);
}}
.sl-row-label {{ min-width: 0; font-weight: var(--fw-medium); }}
.sl-row-label small {{ display: block; font-size: var(--fs-2xs); font-weight: 400; color: var(--c-text-faint); }}
.sl-row-src {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); white-space: nowrap; }}

/* ---------- 行程 ---------- */
.sl-days {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--day-min), 1fr)); gap: var(--space-4); }}
.sl-day {{ border: var(--border-w) solid var(--c-line); border-radius: var(--radius-md); background: var(--c-surface-2); padding: var(--space-4); }}
.sl-day-head {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-2) var(--space-3); margin-bottom: var(--space-3); }}
.sl-day-no {{ font-family: var(--font-mono); font-size: var(--fs-xs); font-weight: var(--fw-bold); color: var(--c-accent); letter-spacing: var(--tracking-caps); }}
.sl-day-name {{ font-size: var(--fs-sm); font-weight: var(--fw-semibold); }}
.sl-day-sum {{ margin-left: auto; flex: 0 0 auto; white-space: nowrap; font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); font-variant-numeric: tabular-nums; }}
.sl-tl {{ display: flex; flex-direction: column; gap: var(--space-2); }}
/* 时间 / 活动 / 金额三段。金额永不折行：窄的时候让它整体换到下一行，
   而不是被挤成一列、把"¥340"拆成"¥ 3 4 0"。 */
.sl-tl-item {{
  display: flex; flex-wrap: wrap; align-items: baseline;
  gap: var(--space-1) var(--space-3);
  position: relative; padding-left: var(--space-3);
  border-left: var(--timeline-w) solid var(--c-line-strong);
}}
.sl-tl-item::before {{
  content: ""; position: absolute; top: 0.5em; left: calc(-1 * (var(--timeline-w) + var(--timeline-dot) / 2));
  width: var(--timeline-dot); height: var(--timeline-dot); border-radius: var(--radius-pill); background: var(--c-accent);
}}
.sl-tl-time {{ flex: 0 0 52px; font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }}
.sl-tl-act {{ flex: 1 1 8rem; min-width: 0; font-size: var(--fs-sm); overflow-wrap: anywhere; }}
.sl-tl-act small {{ display: block; font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); }}
.sl-tl-cost {{
  flex: 0 0 auto; margin-left: auto;
  font-family: var(--font-mono); font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}}

/* ---------- 装备清单（按状态分组的横排 chips） ---------- */
.sl-gear {{ display: flex; flex-direction: column; }}
.sl-gear-line {{
  display: flex; flex-wrap: wrap; align-items: baseline;
  gap: var(--space-2) var(--space-3);
  padding: var(--space-3) var(--space-5);
}}
.sl-gear-line + .sl-gear-line {{ border-top: var(--panel-rule-w) solid var(--c-line); }}
.sl-gear-head {{
  min-width: 52px; font-size: var(--fs-2xs); font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-text-muted);
}}
.sl-gear-head .n {{ margin-left: var(--space-2); font-family: var(--font-mono); color: var(--c-text); }}
.sl-gear-item {{
  display: inline-flex; align-items: baseline; gap: var(--space-2);
  padding: 1px var(--space-2); border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-sm); background: var(--c-surface-2); font-size: var(--fs-sm);
}}
.sl-gear-item b {{ font-weight: var(--fw-medium); }}
.sl-gear-item .qty {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); }}
.sl-gear-item .sl-gear-price {{ font-family: var(--font-mono); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; white-space: nowrap; }}
.sl-gear-item small {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); }}
.sl-gear-foot {{ padding: var(--space-2) var(--space-5); font-size: var(--fs-2xs); color: var(--c-text-muted); background: var(--c-surface-2); border-top: var(--panel-rule-w) solid var(--c-line); }}

/* ---------- 预算（一行数字 + 一条进度，明细压到脚注） ---------- */
.sl-budget {{ display: flex; flex-direction: column; gap: var(--space-3); }}
.sl-budget-row {{ display: grid; grid-template-columns: auto minmax(80px, 1fr) auto auto; align-items: center; gap: var(--space-4); }}
.sl-budget-num span {{ display: block; font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-text-faint); }}
.sl-budget-num b {{ font-family: var(--font-mono); font-size: var(--fs-lg); font-variant-numeric: tabular-nums; white-space: nowrap; }}
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
.sl-budget-foot {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-muted); }}

.sl-kv {{ display: flex; flex-direction: column; gap: var(--space-2); }}
.sl-kv-row {{ display: flex; align-items: baseline; gap: var(--space-3); font-size: var(--fs-xs); }}
.sl-kv-row span {{ color: var(--c-text-muted); }}
.sl-kv-row b {{ margin-left: auto; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }}

/* ---------- 默认值提醒（不是告警，放在整组面板之后） ---------- */
.sl-note {{
  margin-top: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  border: var(--border-w) dashed var(--c-line-strong);
  background: var(--c-surface-2);
  font-size: var(--fs-xs);
  color: var(--c-text-muted);
}}

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
.sl-metric-value {{ font-family: var(--font-display); font-size: var(--fs-xl); line-height: 1.2; font-weight: var(--fw-bold); white-space: nowrap; }}
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

/* ---------- 聊天 ---------- */
[data-testid="stChatMessage"] {{
  background: transparent !important; border: 0 !important; padding: 0 !important;
  gap: 0 !important; margin-bottom: var(--space-5) !important;
}}
[data-testid="stChatMessage"] [data-testid^="stChatMessageAvatar"] {{ display: none !important; }}
[data-testid="stChatMessage"] [data-testid="stChatMessageContent"] {{ width: 100%; }}
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

/* 我的话：自己画的气泡，不依赖 stChatMessage 内部结构。 */
.sl-user-bubble {{
  width: fit-content; max-width: 78%;
  margin: var(--space-2) 0 var(--space-4) auto;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--bubble-radius); border-bottom-right-radius: var(--bubble-tail);
  background: var(--c-user-bubble-bg); color: var(--c-user-bubble-text);
  border: var(--border-w) solid var(--c-line); box-shadow: var(--shadow-2);
  font-size: var(--fs-md); line-height: 1.5;
  white-space: pre-wrap; overflow-wrap: anywhere;
}}

/* ---------- 记忆提取：入口与候选卡片 ---------- */
.st-key-memory_entry {{ margin-top: var(--space-3); }}
.st-key-memory_entry .stButton > button {{
  justify-content: center !important;
  min-height: 32px !important;
  padding: var(--space-1) var(--space-3) !important;
  font-size: var(--fs-xs) !important;
  font-weight: var(--fw-medium) !important;
  letter-spacing: var(--tracking-caps) !important;
  color: var(--c-accent-text) !important;
  background: transparent !important;
  border: var(--border-w) dashed var(--c-accent-line) !important;
}}
.st-key-memory_entry .stButton > button:hover {{
  background: var(--c-accent-soft) !important;
  border-style: solid !important;
}}
.sl-memory-hint {{
  display: flex; align-items: baseline; gap: var(--space-2);
  padding-top: 7px; font-size: var(--fs-xs); color: var(--c-text-muted);
  line-height: var(--lh-tight);
}}
.sl-memory-hint-kicker {{
  font-family: var(--font-mono); font-size: var(--fs-2xs);
  letter-spacing: var(--tracking-caps); text-transform: uppercase;
  color: var(--c-text-faint);
}}

.st-key-memory_panel {{
  margin-top: var(--space-2); padding: var(--space-4);
  border: var(--border-w) solid var(--c-line); border-radius: var(--radius-md);
  background: var(--c-surface); box-shadow: var(--shadow-2);
}}
.st-key-memory_panel > [data-testid="stVerticalBlock"] {{ gap: 0; }}
.sl-memory-head {{ display: flex; flex-direction: column; gap: var(--space-1); padding-bottom: var(--space-3); }}
.sl-memory-head h3 {{
  margin: 0; font-family: var(--font-display); font-size: var(--fs-lg);
  letter-spacing: var(--tracking-display);
}}
.sl-memory-head p {{ margin: 0; font-size: var(--fs-xs); color: var(--c-text-muted); line-height: var(--lh-tight); }}

[class*="st-key-memory_row_"] {{
  padding: var(--space-2) 0;
  border-top: var(--panel-rule-w) solid var(--c-line);
}}
[class*="st-key-memory_row_"] [data-testid="stHorizontalBlock"] {{ align-items: center; gap: var(--space-3); }}
[class*="st-key-memory_row_"] .stButton > button {{
  min-height: 30px !important; padding: 0 var(--space-3) !important;
  font-size: var(--fs-2xs) !important; letter-spacing: var(--tracking-caps) !important;
  white-space: nowrap;
}}
[class*="st-key-memory_row_"][class*="_written"] .stButton > button {{
  background: var(--c-accent-soft) !important;
  border-color: var(--c-accent-line) !important;
  color: var(--c-accent-text) !important;
}}
[class*="st-key-memory_row_"][class*="_written"] .sl-memory-row-text {{ color: var(--c-text-muted); }}

.sl-memory-row-body {{ display: flex; flex-direction: column; gap: 3px; }}
.sl-memory-row-text {{ font-size: var(--fs-sm); color: var(--c-text); line-height: var(--lh-tight); }}
.sl-memory-row-no {{
  display: inline-block; min-width: 1.9em;
  font-family: var(--font-mono); font-size: var(--fs-2xs);
  color: var(--c-accent-text); letter-spacing: var(--tracking-caps);
}}
.sl-memory-row-meta {{ font-size: var(--fs-2xs); color: var(--c-text-faint); letter-spacing: var(--tracking-caps); }}

/* ---------- 输入区 ---------- */
[data-testid="stBottom"],
[data-testid="stBottom"] > div,
[data-testid="stBottomBlockContainer"] > div {{ background: transparent !important; }}
[data-testid="stBottomBlockContainer"] {{ max-width: 100%; padding: 0 var(--space-6) var(--space-4) !important; }}
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
/* 输入框有内容时按钮会变成实心强调色：图标还跟着用强调色就是同色，看不见了。
   反白只在这种"可用"状态下生效，空输入（禁用态）保持浅底强调色。 */
[data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] button:not([disabled]) {{
  color: var(--c-accent-ink) !important;
  background: var(--c-accent) !important;
}}
[data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] button:not([disabled]):hover {{
  background: var(--c-accent-strong) !important;
}}
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
/* 个人中心的可信 Actor：一行说明，不要 st.info 那条左侧强调竖条 */
.sl-actor {{
  display: flex; align-items: baseline; gap: var(--space-2);
  padding: var(--space-1) 0 var(--space-2);
  font-size: var(--fs-sm); color: var(--c-text-muted);
}}
.sl-actor-kicker {{
  font-family: var(--font-mono); font-size: var(--fs-2xs);
  letter-spacing: 0.06em; text-transform: uppercase; color: var(--c-text-faint);
}}
.sl-actor b {{ color: var(--c-text); font-weight: var(--fw-medium); }}

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

/* 过程步骤：台账三态 —— 运行中（赭）/ 已完成（绿）/ 待执行（虚线圈）。
   三态必须一眼分开：运行中实线+底色+实心点，已完成绿线绿点，待执行虚线空心点并压暗。 */
.process-card {{
  position: relative;
  margin: 0 0 3px;
  padding: 3px 0 4px var(--space-3);
  border-left: 3px solid var(--c-line-strong);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}}
/* 竖线上的状态点：中心压在 3px 线宽上 */
.process-card::before {{
  content: ""; position: absolute; top: 5px; left: -6px;
  width: 9px; height: 9px; border-radius: var(--radius-pill);
  background: var(--c-surface); border: 1.5px solid var(--c-line-strong);
}}
.process-card .process-head {{
  display: flex; flex-wrap: wrap; align-items: center;
  gap: var(--space-1) var(--space-2);
}}
.process-card .process-no {{
  font-family: var(--font-mono); font-size: var(--fs-2xs);
  letter-spacing: 0.06em; color: var(--c-text-faint);
  font-variant-numeric: tabular-nums;
}}
.process-card .process-title {{
  font-family: var(--font-mono); font-size: var(--fs-2xs);
  letter-spacing: 0.06em; text-transform: uppercase; color: var(--c-text-faint);
}}
.process-card .process-head .sl-tag {{ margin-left: auto; }}
.process-card .process-copy {{ font-size: var(--fs-xs); color: var(--c-text-muted); line-height: var(--lh-tight); }}
/* 运行中卡片里的「当前动作」：不算正式步骤，只报这一秒在干什么 */
.process-card .process-live {{
  margin-top: 3px; padding-left: var(--space-2);
  border-left: 2px solid var(--c-accent-line);
  font-size: var(--fs-2xs); line-height: var(--lh-tight);
  color: var(--c-accent-text);
}}

/* 运行中：强调色实线 + 左侧渐隐底色 + 实心点带光晕 */
.process-card.active {{
  border-left-color: var(--c-accent);
  background: linear-gradient(90deg, var(--c-accent-soft), transparent 78%);
}}
.process-card.active::before {{
  background: var(--c-accent); border-color: var(--c-accent);
  box-shadow: 0 0 0 3px var(--c-accent-soft);
}}
.process-card.active .process-no {{ color: var(--c-accent); font-weight: var(--fw-bold); }}
.process-card.active .process-title {{ color: var(--c-text); font-weight: var(--fw-semibold); }}
.process-card.active .process-copy {{ color: var(--c-text); }}
/* 运行中的状态签做实体填充，别和卡片的浅赭底糊成一片 */
.process-card.active .sl-tag--hard {{
  background: var(--c-accent); border-color: var(--c-accent);
  color: var(--c-accent-ink); font-weight: var(--fw-semibold);
}}

/* 已完成：橄榄绿实线 + 实心点，标题转绿、描述自然回落到次级色 */
.process-card.completed {{ border-left-color: var(--c-ok-line); }}
.process-card.completed::before {{ background: var(--c-ok); border-color: var(--c-ok); }}
.process-card.completed .process-title {{ color: var(--c-ok); }}

/* 待执行：虚线 + 空心点，整体压暗，不抢运行中的注意力 */
.process-card.pending {{
  border-left-style: dashed; border-left-color: var(--c-line);
  opacity: 0.55;
}}
.process-card.pending::before {{ background: var(--c-surface); border-color: var(--c-line-strong); }}

/* 警告 / 待确认 / 失败 */
.process-card.warning, .process-card.waiting {{ border-left-color: var(--c-warn); }}
.process-card.warning::before, .process-card.waiting::before {{ background: var(--c-warn); border-color: var(--c-warn); }}
.process-card.warning .process-title, .process-card.waiting .process-title {{ color: var(--c-warn); }}
.process-card.error {{ border-left-color: var(--c-danger); }}
.process-card.error::before {{ background: var(--c-danger); border-color: var(--c-danger); }}
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

/* 结构化答案已经出来、正文还在写：只报状态，不铺 Markdown */
.process-hint {{
  display: flex; align-items: center; gap: var(--space-2);
  margin: var(--space-1) 0 var(--space-2);
  font-family: var(--font-mono); font-size: var(--fs-2xs);
  letter-spacing: 0.04em; color: var(--c-text-muted);
}}
.process-hint::before {{
  content: ""; flex: 0 0 auto;
  width: 6px; height: 6px; border-radius: var(--radius-pill);
  background: var(--c-accent); box-shadow: 0 0 0 3px var(--c-accent-soft);
}}

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

/* ---------- 追问卡片：A/B/C 药丸选项 + D 自己填 ---------- */
.st-key-clarify_card {{
  border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-lg);
  background: var(--c-ask-bg);
  padding: var(--space-4);
  box-shadow: var(--shadow-1);
  margin: var(--space-3) 0 var(--space-2);
}}
.sl-ask-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-2); margin-bottom: var(--space-2); }}
.sl-ask-kicker {{ font-family: var(--font-display); font-size: var(--fs-sm); font-weight: var(--fw-bold); color: var(--c-accent-text); }}
.sl-ask-tip {{ font-size: var(--fs-2xs); color: var(--c-text-muted); }}
.sl-ask-q {{ margin: var(--space-3) 0 var(--space-1); font-size: var(--fs-sm); font-weight: var(--fw-semibold); color: var(--c-text); }}
.sl-split-side {{ display: flex; flex-direction: column; gap: 1px; line-height: 1.15; min-width: 0; }}
.sl-split-side span {{ font-family: var(--font-display); font-size: var(--fs-2xs); color: var(--c-text-muted); white-space: nowrap; }}
.sl-split-side b {{ font-family: var(--font-mono); font-size: var(--fs-lg); color: var(--c-accent-text); font-variant-numeric: tabular-nums; }}
.sl-split-side small {{ font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text-faint); font-variant-numeric: tabular-nums; }}
.sl-split-side--right {{ align-items: flex-end; text-align: right; }}
.st-key-clarify_card [data-testid="stSliderTickBar"],
.st-key-clarify_card [data-testid="stSliderThumbValue"] {{ display: none; }}

.st-key-clarify_card [data-testid="stForm"] {{ border: none; padding: 0; }}
.st-key-clarify_card [role="radiogroup"] {{ display: flex; flex-wrap: wrap; gap: var(--space-2); }}
.st-key-clarify_card [role="radiogroup"] > label {{
  margin: 0; padding: 5px 12px; cursor: pointer;
  background: var(--c-ask-option-bg);
  border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-pill);
  font-size: var(--fs-xs); color: var(--c-text);
}}
.st-key-clarify_card [role="radiogroup"] > label:hover {{ border-color: var(--c-ask-option-active-line); }}
.st-key-clarify_card [role="radiogroup"] > label:has(input:checked) {{
  background: var(--c-ask-option-active-bg);
  border-color: var(--c-ask-option-active-line);
  color: var(--c-accent-text); font-weight: var(--fw-semibold);
}}
.st-key-clarify_card [role="radiogroup"] input[type="radio"] {{ display: none; }}
.st-key-clarify_card .stButton > button {{
  background: var(--c-accent); color: var(--c-accent-ink);
  border: var(--border-w) solid var(--c-accent-strong);
  border-radius: var(--radius-pill);
}}

/* ---------- 回答块（answer_blocks）：所有回答共用一套纸面 ---------- */
.sl-answer {{ display: flex; flex-direction: column; gap: var(--space-3); }}
.sl-answer > .sl-panel + .sl-panel {{ margin-top: 0; }}
.sl-answer .sl-lead {{ margin-bottom: 0; }}

.sl-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: var(--space-3); }}
.sl-stat {{
  padding: var(--space-3) var(--space-4);
  border: var(--border-w) solid var(--c-line); border-radius: var(--radius-md);
  background: var(--c-surface);
}}
.sl-stat-label {{ font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-text-faint); }}
.sl-stat-value {{ margin-top: 2px; font-family: var(--font-mono); font-size: var(--fs-lg); font-weight: var(--fw-bold); font-variant-numeric: tabular-nums; white-space: nowrap; }}
.sl-stat-note {{ font-size: var(--fs-2xs); color: var(--c-text-muted); }}

.sl-goods {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(228px, 1fr)); gap: var(--space-3); }}
.sl-good {{
  display: flex; flex-direction: column; gap: var(--space-2);
  padding: var(--space-4); border: var(--border-w) solid var(--c-line);
  border-radius: var(--radius-md); background: var(--c-surface); box-shadow: var(--shadow-2);
}}
.sl-good-head {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-2); }}
.sl-good-kicker {{ font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase; color: var(--c-text-faint); }}
.sl-good-price {{ margin-left: auto; font-family: var(--font-mono); font-size: var(--fs-md); font-weight: var(--fw-bold); white-space: nowrap; font-variant-numeric: tabular-nums; }}
.sl-good-name {{ font-family: var(--font-display); font-size: var(--fs-md); font-weight: var(--fw-bold); line-height: 1.35; }}
.sl-good-note {{ font-size: var(--fs-xs); color: var(--c-text-muted); }}
.sl-good-tags {{ display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: auto; padding-top: var(--space-2); }}

.sl-tbl {{ width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }}
.sl-tbl th {{
  padding: 0 0 var(--space-2); border-bottom: var(--panel-rule-w) solid var(--c-line-strong);
  font-size: var(--fs-2xs); font-weight: var(--fw-bold); letter-spacing: var(--tracking-caps);
  text-transform: uppercase; color: var(--c-text-faint); text-align: left; white-space: nowrap;
}}
.sl-tbl td {{ padding: var(--space-2) 0; border-bottom: var(--panel-rule-w) solid var(--c-line); overflow-wrap: anywhere; }}
.sl-tbl tr:last-child td {{ border-bottom: 0; }}
.sl-tbl .num {{ text-align: right; }}
.sl-tbl td.num {{ font-family: var(--font-mono); font-variant-numeric: tabular-nums; white-space: nowrap; }}

/* 正文块：没有结构化数据的回答也不能裸奔 */
[class*="st-key-answer_text_"] {{
  padding: var(--space-4) var(--space-5);
  border: var(--border-w) solid var(--c-line); border-radius: var(--radius-md);
  background: var(--c-surface);
}}
.sl-prose {{ font-size: var(--fs-md); line-height: var(--lh-normal); }}
.sl-prose > :first-child {{ margin-top: 0 !important; }}
.sl-prose > :last-child {{ margin-bottom: 0 !important; }}
.sl-prose h2, [class*="st-key-answer_text_"] h2 {{
  margin: var(--space-4) 0 var(--space-2); padding-top: var(--space-3);
  border-top: var(--panel-rule-w) solid var(--c-line);
  font-family: var(--font-display); font-size: var(--fs-lg);
}}
.sl-prose h3, [class*="st-key-answer_text_"] h3 {{ margin: var(--space-3) 0 var(--space-1); font-size: var(--fs-md); }}
.sl-prose p, [class*="st-key-answer_text_"] p {{ margin: var(--space-2) 0; }}
.sl-prose ul, .sl-prose ol, [class*="st-key-answer_text_"] ul, [class*="st-key-answer_text_"] ol {{ margin: var(--space-2) 0; padding-left: 1.2rem; }}
.sl-prose li, [class*="st-key-answer_text_"] li {{ margin: 2px 0; font-size: var(--fs-sm); }}
.sl-prose blockquote, [class*="st-key-answer_text_"] blockquote {{
  margin: var(--space-3) 0; padding: var(--space-2) var(--space-4);
  border-left: 2px solid var(--c-accent-line); background: var(--c-surface-2); color: var(--c-text-muted);
}}
.sl-prose code, [class*="st-key-answer_text_"] code {{
  font-family: var(--font-mono); font-size: var(--fs-xs);
  padding: 1px var(--space-1); border-radius: var(--radius-xs); background: var(--c-chip-bg);
}}
.sl-prose hr, [class*="st-key-answer_text_"] hr {{ border: 0; border-top: var(--panel-rule-w) solid var(--c-line); margin: var(--space-4) 0; }}
.sl-prose table, [class*="st-key-answer_text_"] table {{ width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }}
.sl-prose th, [class*="st-key-answer_text_"] th {{
  padding: 0 0 var(--space-2); border-bottom: var(--panel-rule-w) solid var(--c-line-strong);
  font-size: var(--fs-2xs); letter-spacing: var(--tracking-caps); text-transform: uppercase;
  color: var(--c-text-faint); text-align: left;
}}
.sl-prose td, [class*="st-key-answer_text_"] td {{ padding: var(--space-2) 0; border-bottom: var(--panel-rule-w) solid var(--c-line); }}
.sl-prose tr:last-child td, [class*="st-key-answer_text_"] tr:last-child td {{ border-bottom: 0; }}

/* ---------- 响应式 ---------- */
@media (max-width: 900px) {{
  [data-testid="stMainBlockContainer"] {{ padding-left: var(--space-4); padding-right: var(--space-4); }}
  [data-testid="stExpandSidebarButton"] {{ top: 10px; left: 10px; }}
  [data-testid="stMainBlockContainer"] {{ padding-top: calc(var(--space-4) + var(--space-3)); }}
  .sl-page-head {{ padding: var(--space-3) var(--space-4); }}
  [data-testid="stBottomBlockContainer"] {{ padding-left: var(--space-4) !important; padding-right: var(--space-4) !important; }}
  .sl-rows {{ grid-template-columns: minmax(0, 1fr); }}
  .sl-row {{ grid-template-columns: minmax(0, 1fr) auto; }}
  .sl-budget-row {{ grid-template-columns: auto minmax(0, 1fr) auto; }}
  .sl-budget-row .sl-delta {{ grid-column: 1 / -1; align-items: flex-start; margin-left: 0; }}
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
    # 没有标签时不留空槽位，否则页头右侧会多出 2px 空白。
    meta_html = f'<div class="sl-tag sl-tag--pill">{_e(meta)}</div>' if meta else ""
    meta_slot = f'<div class="sl-page-meta">{meta_html}</div>' if meta_html else ""
    desc_html = f"<p>{_e(description)}</p>" if description else ""
    st.markdown(
        f"""
        <div class="sl-page-head">
          <div>{kicker_html}<h1>{_e(title)}</h1>{desc_html}</div>
          {meta_slot}
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
        '<div class="sl-brand">'
        '<div><div class="sl-brand-name">SmartLife</div>'
        '<div class="sl-brand-sub">Trip &amp; Gear</div></div></div>'
    )


def nav_label_html(text: str) -> str:
    return f'<div class="sl-nav-label">{_e(text)}</div>'


def user_bubble_html(text: str) -> str:
    """我的话：自绘气泡。

    ``st.chat_message`` 的内部结构各版本不一样，用 CSS 去改它的子节点风险太大，
    所以用户消息不走 chat_message，直接输出一个受控的 div。
    """
    return f'<div class="sl-user-bubble">{_e(str(text or ""))}</div>'


def user_html(user_id: str, role: str = "") -> str:
    return (
        f'<div class="sl-user">'
        f'<div><div class="sl-user-name">{_e(user_id)}</div>'
        f'<div class="sl-user-role">{_e(role or "local actor")}</div></div></div>'
    )
# ---------- 行程装备结果（plan） ----------
def _constraint_rows(constraints: Sequence[Dict[str, Any]]) -> str:
    """只展示"内容 + 来源"。

    ``hard`` / ``status`` 仍在数据里（verify 判 critical、verdict 算权重都要用），
    但展示层不再开两列：用户关心的是"我提了哪些要求、它们从哪来"，
    至于系统内部把它标成硬还是软、校验通过没有，是 05 面板要说的事。
    """
    rows = []
    for item in constraints:
        rows.append(
            '<div class="sl-row">'
            f'<span class="sl-row-label">{_e(item.get("text", ""))}</span>'
            f'<span class="sl-row-src">{_e(item.get("source", ""))}</span>'
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
    # 当天小计同样要算住宿：只算条目会让"每天 ¥1480"和面板合计对不上
    try:
        subtotal += float(day.get("lodging_cost") or 0)
    except (TypeError, ValueError):
        pass
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


def _budget_blocked(item: Dict[str, Any]) -> bool:
    """预算内没挑到款的那一项：清单里不占行，只在脚注交代。"""
    return "预算不足" in str(item.get("reason") or "")


def _gear_counts(gear: Sequence[Dict[str, Any]]) -> tuple[int, int]:
    """清单只由商品库决定：库里没有的条目在 shop 阶段就已被移除。

    买不起的不算"待购"：它不是这次要买的东西，列进清单等于给用户看一份
    他买不到的计划。数量仍在脚注里如实交代。
    """
    owned = sum(1 for item in gear if item.get("status") == "owned")
    need = sum(
        1 for item in gear
        if item.get("status") == "need_buy" and not _budget_blocked(item)
    )
    return owned, need


def _gear_columns(gear: Sequence[Dict[str, Any]], products: Dict[str, Any]) -> tuple[str, str]:
    """已有 / 待购两组横排；条目少时不占空列，条目多时自动换行。"""
    owned = [item for item in gear if item.get("status") == "owned"]
    need_all = [item for item in gear if item.get("status") == "need_buy"]
    # 买不起的不进清单，但要在脚注里点名，否则等于悄悄砍需求
    budget_blocked = [item for item in need_all if _budget_blocked(item)]
    need = [item for item in need_all if not _budget_blocked(item)]
    skipped = [item for item in gear if item.get("status") == "skipped"]

    lines = []
    for key, items in (("owned", owned), ("need_buy", need)):
        if not items:
            continue
        chips = []
        for item in items:
            product = (products.get(item.get("name")) or {}) if key == "need_buy" else {}
            extra = ""
            title = str(item.get("name", ""))
            note = ""
            if product.get("price"):
                product_name = str(product.get("name") or "")
                brand = str(product.get("brand") or "")
                if product_name:
                    title = product_name
                extra = f'<span class="sl-gear-price">{_money(product.get("price"))}</span>'
                if brand and brand not in product_name:
                    extra += f"<small>{_e(brand)}</small>"
                match = product.get("match") or {}
                if product_name and product_name != item.get("name"):
                    note = f"需求：{item.get('name', '')}"
                if match.get("kind") == "substitute":
                    query = match.get("query") or ""
                    note = (note + " · " if note else "") + f"按「{query}」检索到的同类款"
            reason = item.get("reason") or ""
            qty = item.get("qty") or 1
            chips.append(
                '<span class="sl-gear-item">'
                f'<b>{_e(title)}</b>'
                f'<span class="qty">×{_e(qty)}</span>'
                f"{extra}"
                + (f"<small>{_e(note)}</small>" if note else "")
                + (f"<small>{_e(reason)}</small>" if reason else "")
                + "</span>"
            )
        lines.append(
            f'<div class="sl-gear-line"><span class="sl-gear-head">{GEAR_STATUS_LABEL[key]}'
            f'<span class="n">{len(items)}</span></span>{"".join(chips)}</div>'
        )

    html = f'<div class="sl-gear">{"".join(lines)}</div>' if lines else ""

    foot_lines = []
    if budget_blocked:
        names = "、".join(str(item.get("name", "")) for item in budget_blocked)
        foot_lines.append(f"预算内没选到：{names}（共 {len(budget_blocked)} 项，未计入预算）")
    if skipped:
        foot_lines.append("已按预算省略：" + "、".join(str(item.get("name", "")) for item in skipped))
    skipped_names = {str(item.get("name") or "") for item in skipped}
    wanted = [name for name in ("连衣裙", "泳衣") if name in skipped_names]
    if wanted:
        foot_lines.append(
            f"想保留{'/'.join(wanted)}：把装备占比调高（如 20%），或去掉雨伞/防晒再试"
        )
    foot = "".join(f'<div class="sl-gear-foot">{_e(line)}</div>' for line in foot_lines)
    return html, foot


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
    # 行程合计必须和 verify 用同一个口径：条目花费 + 住宿费 + 城际交通估算。
    # 以前只加条目花费，于是面板写"没超预算"、05 却报"超出预算"。
    trip_total = 0.0
    for day in days:
        for entry in day.get("items", []) or []:
            try:
                trip_total += float(entry.get("cost") or 0)
            except (TypeError, ValueError):
                pass
        try:
            trip_total += float(day.get("lodging_cost") or 0)
        except (TypeError, ValueError):
            pass
    try:
        intercity_total = float(itinerary.get("intercity_cost") or 0)
    except (TypeError, ValueError):
        intercity_total = 0.0
    trip_total += intercity_total

    gear_total = 0.0
    for item in gear:
        if item.get("status") != "need_buy":
            continue
        product = products.get(item.get("name")) or {}
        if not product.get("price"):
            # 库里没有的不计价，也不单独占一行（装备清单里已用一句话说明）
            continue
        try:
            gear_total += float(product["price"]) * float(item.get("qty") or 1)
        except (TypeError, ValueError):
            continue
    total = trip_total + gear_total
    budget = facts.get("budget")
    has_budget = isinstance(budget, (int, float))
    remaining = (float(budget) - total) if has_budget else None
    raw_pct = 0.0
    if has_budget and budget:
        raw_pct = max(0.0, total / float(budget) * 100)
    # 进度条截在 100%，但文字必须显示真实百分比（超支 144% 不能写成 100%）。
    pct = min(100.0, raw_pct)

    parts = [
        panel_html(
            "01",
            "约束清单",
            f"{len(constraints)} 条",
            _constraint_rows(constraints),
            flush=True,
        )
    ]

    if days:
        day_html = "".join(_day_html(day) for day in days)
        intercity_note = f"（含城际估算 {_money(intercity_total)}）" if intercity_total else ""
        parts.append(
            panel_html(
                "02",
                "逐日行程",
                f"{len(days)} 天 · 行程合计 {_money(trip_total)}{intercity_note}",
                f'<div class="sl-days">{day_html}</div>',
            )
        )

    if gear:
        columns, foot = _gear_columns(gear, products)
        owned, need = _gear_counts(gear)
        if columns or foot:
            parts.append(
                panel_html(
                    "03",
                    "装备清单",
                    f"已有 {owned} / 待购 {need}",
                    f'{columns}{foot}',
                )
            )

    over = remaining is not None and remaining < 0
    bar_class = "sl-bar-fill sl-bar-fill--over" if over else "sl-bar-fill"
    delta_class = "sl-delta sl-delta--over" if over else "sl-delta"
    if remaining is None:
        delta_label, delta_text = "剩余", "未设预算"
    elif over:
        delta_label, delta_text = "超支", _money(abs(remaining))
    else:
        delta_label, delta_text = "剩余", f"+{_money(remaining)}"
    scope = "含装备" if (facts.get("budget_scope") or "trip_and_gear") != "trip_only" else "不含装备"
    # 明细压到脚注：上面一行只留"花了多少 / 剩多少"。
    # 两端的"额度 + 占比"必须写在脚注里：滑块上只回一个百分比，用户看不出
    # 行程和装备各拿了多少钱；这里和滑块用同一套 split 口径。
    trip_foot = f"行程 {_money(trip_total)}"
    gear_foot = f"装备 {_money(gear_total)}"
    trip_cap = facts.get("trip_cap")
    gear_cap = facts.get("gear_cap")

    def _share(part: float, whole: float) -> str:
        return f"{part / whole:.0%}" if whole else "—"

    if has_budget and budget:
        if isinstance(trip_cap, (int, float)):
            trip_foot += (
                f"（额度 {_money(trip_cap)} · 分配 {_share(float(trip_cap), float(budget))}"
                f" · 已用 {_share(trip_total, float(trip_cap))}）"
            )
        if isinstance(gear_cap, (int, float)):
            gear_foot += (
                f"（额度 {_money(gear_cap)} · 分配 {_share(float(gear_cap), float(budget))}"
                f" · 已用 {_share(gear_total, float(gear_cap))}）"
            )
    foot = f"{trip_foot} · {gear_foot} · 口径 {scope}"
    if has_budget:
        foot += f" · 已用 {raw_pct:.0f}%"
    estimate_notes = itinerary.get("estimate_notes") or []
    estimate_html = ""
    if estimate_notes:
        estimate_html = (
            '<div class="sl-budget-foot sl-budget-estimate">'
            + _e("；".join(str(note) for note in estimate_notes))
            + "</div>"
        )
    parts.append(
        panel_html(
            "04",
            "预算占用",
            f"口径：{scope}",
            f"""
            <div class="sl-budget">
              <div class="sl-budget-row">
                <div class="sl-budget-num"><span>合计</span><b>{_money(total)}</b></div>
                <div class="sl-bar"><div class="{bar_class}" style="width:{pct:.1f}%"></div></div>
                <div class="sl-budget-num"><span>预算</span><b>{_money(budget) if has_budget else "未设"}</b></div>
                <div class="{delta_class}"><span>{delta_label}</span><b>{_e(delta_text)}</b></div>
              </div>
              <div class="sl-budget-foot">{_e(foot)}</div>
              {estimate_html}
            </div>
            """,
        )
    )

    # 商品库未覆盖的装备不是约束冲突，单独并成一条提示，避免刷屏。
    unmatched = [item for item in issues if "未匹配" in str(item.get("hint") or "")]
    # 只留"改了结论"的硬冲突（时间重叠、温标不够、天数不符、无货）。
    # "预算内没选到款"是 minor，03 的脚注已经点名，再在 05 说一遍就是同一件事
    # 讲两次；全通过时也不再摆一块"未发现约束冲突"占位。
    hard = [
        item for item in issues
        if item not in unmatched and item.get("severity") != "minor"
    ]

    alerts = []
    pickup = [
        issue for issue in issues
        if "可到店自提" in str(issue.get("constraint") or "")
    ]
    if pickup:
        names = "、".join(
            str(issue.get("constraint", "")).replace("装备：", "")
            for issue in pickup[:6]
        )
        more = f" 等 {len(pickup)} 项" if len(pickup) > 6 else ""
        alerts.append(alert_html(
            "warn",
            "可到店自提（不判不可行）",
            f"{names}{more}；行程里已有商场/到店窗口，按同城有货假设，到店买即可。",
        ))
    for issue in hard[:4]:
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
    # 结论放在最前面：不能等用户自己从 05 的几条 critical 里推断"不可行"。
    banner = _verdict_banner(payload.get("verdict") or {})
    if banner:
        parts.insert(0, banner)
    # 默认值不是告警，但必须说出口：人数、出发日期都可能只是"先这么算"。
    parts.append(_defaults_note_html(facts))
    return "".join(parts)


def _verdict_banner(verdict: Dict[str, Any]) -> str:
    state = str(verdict.get("state") or "")
    blocking = [str(item) for item in (verdict.get("blocking") or [])]
    ask = verdict.get("ask") or {}
    if state == "infeasible":
        detail = "；".join(blocking[:3])
        return alert_html(
            "danger",
            "当前条件不可行",
            (detail + "。" if detail else "")
            + "先改预算/日期/交通，或按下面的放弃清单重排；不要直接照这份方案执行。",
        )
    if state == "needs_input":
        question = str(ask.get("question") or "")
        detail = question or "；".join(blocking[:3])
        return alert_html(
            "warn",
            "需要你确认",
            (detail + "。" if detail else "")
            + "这不是系统故障，是当前条件下只能二选一。",
        )
    if state == "repairable":
        return alert_html(
            "warn",
            "可以按代价调整",
            "有能消除硬冲突的改法；确认后我会按代价从小到大执行。",
        )
    return ""


def _defaults_note_html(facts: Dict[str, Any]) -> str:
    """把本次用了默认值的项摆到面板末尾，给用户一句话改掉的机会。"""
    bits = []
    if not facts.get("people"):
        bits.append("人数（按 1 人计）")
    if not facts.get("start_date"):
        bits.append(f"出发日期（按今天 {date.today():%Y-%m-%d} 计）")
    if not bits:
        return ""
    return (
        '<div class="sl-note">'
        f"默认值：{'、'.join(bits)}。如果和实际不符，回一句我重排。"
        "</div>"
    )


# ============================================================
# 回答块（answer_blocks）
# 所有回答共用一套纸面：有结构化数据就画对应的块，没有就画正文块。
# 块类型：lead / stats / panel / timeline / cards / table / text
# ============================================================

def _rows_html(rows: Iterable[Dict[str, Any]]) -> str:
    out = []
    for row in rows or []:
        sub = f'<small>{_e(row.get("sub"))}</small>' if row.get("sub") else ""
        out.append(
            '<div class="sl-tl-item">'
            f'<span class="sl-tl-time">{_e(row.get("time") or "")}</span>'
            f'<span class="sl-tl-act">{_e(row.get("main") or "")}{sub}</span>'
            f'<span class="sl-tl-cost">{_e(row.get("right") or "—")}</span>'
            "</div>"
        )
    if not out:
        out.append(
            '<div class="sl-tl-item"><span class="sl-tl-time">—</span>'
            '<span class="sl-tl-act">暂无安排</span><span class="sl-tl-cost">—</span></div>'
        )
    return "".join(out)


def _wrapped_panel(block: Dict[str, Any], body: str) -> str:
    """有标题就套 01–05 那种面板壳，没有就裸块。"""
    if not block.get("title"):
        return body
    return panel_html(
        str(block.get("index") or ""),
        str(block.get("title") or ""),
        str(block.get("meta") or ""),
        body,
        flush=bool(block.get("flush")),
    )


def _lead_block_html(block: Dict[str, Any]) -> str:
    em = f"<em>{_e(block['em'])}</em>" if block.get("em") else ""
    note = f"<span>{_e(block['note'])}</span>" if block.get("note") else ""
    return f'<div class="sl-lead">{_e(block.get("text") or "")}{em}{note}</div>'


def _stats_block_html(block: Dict[str, Any]) -> str:
    items = block.get("items") or []
    if not items:
        return ""
    cells = []
    for item in items:
        note = f'<div class="sl-stat-note">{_e(item["note"])}</div>' if item.get("note") else ""
        cells.append(
            '<div class="sl-stat">'
            f'<div class="sl-stat-label">{_e(item.get("label") or "")}</div>'
            f'<div class="sl-stat-value">{_e(item.get("value") or "")}</div>{note}</div>'
        )
    return f'<div class="sl-stats">{"".join(cells)}</div>'


def _timeline_block_html(block: Dict[str, Any]) -> str:
    cards = []
    for item in block.get("items") or []:
        cards.append(
            '<div class="sl-day">'
            f'<div class="sl-day-head"><span class="sl-day-no">{_e(item.get("no") or "")}</span>'
            f'<span class="sl-day-name">{_e(item.get("title") or "")}</span>'
            f'<span class="sl-day-sum">{_e(item.get("meta") or "")}</span></div>'
            f'<div class="sl-tl">{_rows_html(item.get("rows") or [])}</div></div>'
        )
    if not cards:
        return ""
    return _wrapped_panel(block, f'<div class="sl-days">{"".join(cards)}</div>')


def _cards_block_html(block: Dict[str, Any]) -> str:
    cards = []
    for item in block.get("items") or []:
        tags = "".join(
            f'<span class="sl-tag sl-tag--pill">{_e(tag)}</span>'
            for tag in item.get("tags") or []
        )
        note = f'<div class="sl-good-note">{_e(item["note"])}</div>' if item.get("note") else ""
        cards.append(
            '<div class="sl-good">'
            '<div class="sl-good-head">'
            f'<span class="sl-good-kicker">{_e(item.get("kicker") or "")}</span>'
            f'<span class="sl-good-price">{_e(item.get("price") or "")}</span></div>'
            f'<div class="sl-good-name">{_e(item.get("name") or "")}</div>'
            f"{note}"
            f'<div class="sl-good-tags">{tags}</div></div>'
        )
    if not cards:
        return ""
    return _wrapped_panel(block, f'<div class="sl-goods">{"".join(cards)}</div>')


def num_class(numeric: bool) -> str:
    return ' class="num"' if numeric else ""


def _table_block_html(block: Dict[str, Any]) -> str:
    columns = block.get("columns") or []
    if not columns:
        return ""
    numeric = [bool(col.get("num")) for col in columns]
    head = "".join(
        f"<th{num_class(numeric[index])}>{_e(col.get('label') or '')}</th>"
        for index, col in enumerate(columns)
    )
    rows = []
    for row in block.get("rows") or []:
        cells = []
        for index, cell in enumerate(row):
            num = numeric[index] if index < len(numeric) else False
            cells.append(f"<td{num_class(num)}>{_e(cell)}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    body = (
        '<table class="sl-tbl"><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    return _wrapped_panel(block, body)


def answer_block_html(block: Dict[str, Any]) -> str:
    """单个块 → HTML。text 块不经这里（它是 Markdown，交给 Streamlit）。"""
    kind = str(block.get("type") or "")
    if kind == "lead":
        return _lead_block_html(block)
    if kind == "stats":
        return _stats_block_html(block)
    if kind == "timeline":
        return _timeline_block_html(block)
    if kind == "cards":
        return _cards_block_html(block)
    if kind == "table":
        return _table_block_html(block)
    if kind == "panel":
        return panel_html(
            str(block.get("index") or ""),
            str(block.get("title") or ""),
            str(block.get("meta") or ""),
            str(block.get("body") or ""),
            flush=bool(block.get("flush")),
        )
    return ""


def answer_blocks_html(blocks: Sequence[Dict[str, Any]]) -> str:
    parts = [
        answer_block_html(block)
        for block in blocks or []
        if str(block.get("type") or "") != "text"
    ]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    return f'<div class="sl-answer">{"".join(parts)}</div>'


def render_answer_blocks(blocks: Sequence[Dict[str, Any]], *, key: str = "answer") -> None:
    """按顺序渲染：HTML 块合并成一次 markdown，正文块进 keyed 容器吃同一张纸面。"""
    html_buffer: List[str] = []
    text_index = 0

    def _flush() -> None:
        nonlocal html_buffer
        if html_buffer:
            st.markdown(
                f'<div class="sl-answer">{"".join(html_buffer)}</div>',
                unsafe_allow_html=True,
            )
            html_buffer = []

    for block in blocks or []:
        if str(block.get("type") or "") == "text":
            _flush()
            with st.container(key=f"{key}_text_{text_index}"):
                text_index += 1
                if block.get("title"):
                    st.markdown(f"#### {block['title']}")
                st.markdown(str(block.get("text") or ""))
        else:
            piece = answer_block_html(block)
            if piece:
                html_buffer.append(piece)
    _flush()
