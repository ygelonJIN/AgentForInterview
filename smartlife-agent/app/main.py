"""
SmartLife Agent v2 - Streaming 前端应用
全链路实时展示：分类 → 路由 → 工具调用 → 流式生成

记忆系统改动：
- 去掉自动压缩（每20条消息）
- 去掉自动保存事件
- 用户手动触发保存
- 对话结束后提取候选记忆，用户决定是否保留
- 向量库 + MD 文档并存
"""
import streamlit as st
import json
import sys
import os
import importlib
import threading
import time
import queue as thread_queue
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Streamlit 每次 rerun 只重跑主脚本，不会重载被 import 的模块。
# 不改这一条，theme.py 的新函数会 ImportError、新 CSS 也上不了页面，
# 只能靠手动重启；这里每次 rerun 强制同步一次（实测 0.2ms）。
import app.ui.theme as _theme_module

importlib.reload(_theme_module)

from app.config import set_main_config, set_small_config, set_embedding_config, get_embedding_status
from app.identity import ActorContext
from app.observability import log_exception
from app.async_runner import SharedAsyncRunner
from app.stream_response import StreamResponseBuffer
from app.streaming import deduplicate_process_events, reconcile_tool_events
from app.process_timeline import build_process_timeline, infer_active_step
from app.ui.content import filter_travel_resources, load_orders, load_product, load_product_reviews, load_product_summary, load_product_taxonomy, load_products, load_travel_resources, load_user
from app.ui.theme import (
    NAV_ITEMS,
    alert_html,
    brand_html,
    inject_theme,
    metric_columns,
    nav_label_html,
    plan_panels_html,
    render_card,
    render_empty,
    render_metric,
    render_page_header,
    render_review,
    render_section_header,
    user_bubble_html,
    user_html,
)

st.set_page_config(
    page_title="SmartLife Agent v2",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ========== 全局视觉规范 ==========
inject_theme()


# ========== 会话初始化（防御式） ==========
if "messages" not in st.session_state:
    st.session_state["messages"] = []
if "user_id" not in st.session_state:
    st.session_state["user_id"] = os.environ.get("SMARTLIFE_ACTOR_ID", "user_001").strip() or "user_001"
if "browser_session_id" not in st.session_state:
    st.session_state["browser_session_id"] = uuid.uuid4().hex
if "actor_context" not in st.session_state:
    st.session_state["actor_context"] = ActorContext.local(
        st.session_state["user_id"],
        st.session_state["browser_session_id"],
    )
if "turn_graph" not in st.session_state:
    st.session_state["turn_graph"] = None
if "candidate_memories" not in st.session_state:
    st.session_state["candidate_memories"] = []
if "show_memory_extraction" not in st.session_state:
    st.session_state["show_memory_extraction"] = False
if "memory_diag" not in st.session_state:
    st.session_state["memory_diag"] = ""
if "memory_extraction_error" not in st.session_state:
    st.session_state["memory_extraction_error"] = False
if "memory_extraction_scene" not in st.session_state:
    st.session_state["memory_extraction_scene"] = ""
if "memory_saved_map" not in st.session_state:
    # 候选记忆逐条的写入状态：索引 -> memory_id
    st.session_state["memory_saved_map"] = {}
if "memory_extraction_thread" not in st.session_state:
    st.session_state["memory_extraction_thread"] = ""
if "last_memory_extract" not in st.session_state:
    # 本轮"要不要写记忆"的判断结果；空候选时界面也要有个交代，不能什么都不显示。
    st.session_state["last_memory_extract"] = {}
if "pending_compressed_summary" not in st.session_state:
    st.session_state["pending_compressed_summary"] = ""
if "sensitive_action_service" not in st.session_state:
    st.session_state["sensitive_action_service"] = None
if "assistant_msgs" not in st.session_state:
    st.session_state["assistant_msgs"] = []
if "active_thread_id" not in st.session_state:
    st.session_state["active_thread_id"] = ""


def _migrate_legacy_in_memory_conversations():
    """只在本会话第一次渲染时补一次旧内存消息，避免升级后丢历史。

    这个函数挂在模块顶层，每次 rerun 都会执行；不设一次性开关的话，
    内存顺序和库里顺序只要错开一条，去重就会从错位处把后面的消息
    反复写进库（追问补答那一轮曾因此每刷新一次就多一份）。
    """
    from app.memory.conversation_store import ConversationStore
    from app.session import build_thread_id

    if st.session_state.get("legacy_memory_migrated"):
        return []
    st.session_state["legacy_memory_migrated"] = True

    store = ConversationStore()
    migrations = []
    for scene, key in (
        ("general", "assistant_msgs"),
        ("general", "messages"),
    ):
        messages = st.session_state.get(key) or []
        if not isinstance(messages, list) or not messages:
            continue
        thread_id = build_thread_id(
            st.session_state["user_id"],
            scene,
            st.session_state["browser_session_id"],
        )
        migrations.append({
            "thread_id": thread_id,
            "key": key,
            **store.migrate_messages(
                thread_id,
                messages,
                metadata={"scene": scene, "legacy_session_key": key},
            ),
        })
    return migrations


_migrate_legacy_in_memory_conversations()

# ========== 顶层图入口 ==========
def get_turn_graph():
    """UI 唯一入口：返回编译后的对话图。"""
    if st.session_state["turn_graph"] is None:
        try:
            from app.config import has_api_key
            if not has_api_key("main"):
                st.error("❌ 请先在左侧输入 API Key")
                return None
            from app.graphs.orchestrator import get_turn_graph as build_turn_graph_entry
            st.session_state["turn_graph"] = build_turn_graph_entry(model_name=None)
        except Exception as e:
            log_exception("ui.graph_init", e)
            st.error(f"初始化失败: {e}")
            return None
    return st.session_state["turn_graph"]


def get_sensitive_action_service():
    if st.session_state.get("sensitive_action_service") is None:
        from app.memory.repository import get_memory_repository
        from app.sensitive_actions import SensitiveActionService
        st.session_state["sensitive_action_service"] = SensitiveActionService(
            memory_repository=get_memory_repository()
        )
    return st.session_state["sensitive_action_service"]


def _save_sensitive_result(result, action_type):
    if action_type == "memory_save_summary":
        st.session_state.pop("pending_conversation_summary", None)
    receipt = result.get("state", {}).get("result") or {}
    operation_result = receipt.get("result", receipt) if isinstance(receipt, dict) else receipt
    st.session_state["last_memory_action_result"] = {
        "status": result.get("status", "completed"),
        "action_type": action_type,
        "result": operation_result,
    }


def render_inline_sensitive_action(
    action_type,
    payload,
    description,
    *,
    key,
    submit_label,
    primary=False,
):
    """在操作发生的位置完成敏感操作确认，不跳到页面顶部。"""
    state_key = f"pending_inline_{key}"
    pending = st.session_state.get(state_key)
    if not pending:
        if st.button(
            submit_label,
            key=f"request_inline_{key}",
            type="primary" if primary else "secondary",
            use_container_width=True,
        ):
            st.session_state[state_key] = {
                "action_type": action_type,
                "payload": payload,
                "description": description,
            }
            st.rerun()
        return

    st.warning(f"确认执行：{pending['description']}？")
    approve_col, reject_col = st.columns(2)
    with approve_col:
        if st.button("同意", key=f"approve_inline_{key}", type="primary", use_container_width=True):
            service = get_sensitive_action_service()
            try:
                result = service.resume(pending.get("approval_id") or _start_inline_sensitive_action(pending), {
                    "action": "approve",
                    "idempotency_key": pending.get("payload", {}).get("idempotency_key"),
                })
                _save_sensitive_result(result, pending.get("action_type"))
            except Exception as exc:
                st.session_state["last_memory_action_result"] = {
                    "status": "error",
                    "action_type": pending.get("action_type"),
                    "message": f"{type(exc).__name__}: {exc}",
                }
            st.session_state[state_key] = None
            st.rerun()
    with reject_col:
        if st.button("拒绝", key=f"reject_inline_{key}", use_container_width=True):
            approval_id = pending.get("approval_id") or _start_inline_sensitive_action(pending)
            get_sensitive_action_service().resume(approval_id, {"action": "reject"})
            st.session_state[state_key] = None
            st.session_state["last_memory_action_result"] = {
                "status": "rejected",
                "action_type": pending.get("action_type"),
                "result": {},
            }
            st.rerun()


def _start_inline_sensitive_action(pending):
    result = get_sensitive_action_service().start(
        pending["action_type"],
        st.session_state["user_id"],
        pending.get("payload", {}),
    )
    pending["approval_id"] = result["approval_id"]
    return pending["approval_id"]


def _clear_memory_extraction_state():
    st.session_state["candidate_memories"] = []
    st.session_state["memory_saved_map"] = {}
    st.session_state["memory_diag"] = ""
    st.session_state["memory_extraction_error"] = False
    st.session_state["show_memory_extraction"] = False
    st.session_state["memory_extraction_scene"] = ""
    st.session_state["memory_extraction_thread"] = ""


# 按需提取：点一次按钮等一次模型，最多等这么久。
MEMORY_WAIT_SECONDS = 25


def _set_memory_extract_info(scene: str, state: str, *, count: int = 0, note: str = "", thread_id: str = ""):
    """记下"上一次点提取"的结果，供按钮旁边那行说明展示。"""
    st.session_state["last_memory_extract"] = {
        "scene": scene,
        "state": state,
        "count": int(count),
        "note": note,
        "thread_id": thread_id,
        "at": time.strftime("%H:%M"),
    }


def _finish_memory_extract(note: str):
    """候选记忆已经处理完：说明改成结论。"""
    info = dict(st.session_state.get("last_memory_extract") or {})
    info.update({"state": "done", "note": note, "count": 0})
    st.session_state["last_memory_extract"] = info


def _memory_progress(thread_id: str) -> tuple:
    """(游标, 游标之后的用户消息数)；只读库，不建图、不调模型。"""
    if not thread_id:
        return 0, 0
    try:
        from app.memory.conversation_store import ConversationStore

        store = ConversationStore()
        cursor = store.get_extraction_cursor(thread_id)
        rows = store.list_messages(thread_id)
    except Exception:
        return 0, 0
    pending = sum(
        1
        for row in rows
        if str(row.get("role") or "") == "user" and int(row.get("id") or 0) > cursor
    )
    return cursor, pending


def _memory_entry_hint(thread_id: str) -> str:
    """按钮旁边那句话：还有多少没提取 + 上次点完是什么结果。"""
    cursor, pending = _memory_progress(thread_id)
    info = st.session_state.get("last_memory_extract") or {}
    if not cursor and pending:
        parts = [f"还没提取过，共 {pending} 条"]
    elif pending:
        parts = [f"有 {pending} 条新消息"]
    elif info.get("state") == "done":
        parts = ["这个会话已处理完"]
    else:
        parts = ["没有待提取的新消息"]

    tail = {
        "no_content": "上次没提取到可写入的事实",
        "no_new": "上次点击时没有新内容",
        "timeout": "上次提取超时，可以再点一次",
        "error": "上次提取失败，诊断见下方",
        "candidates": f"上次提取到 {info.get('count', 0)} 条",
        "done": info.get("note") or "已处理",
    }.get(info.get("state"))
    if tail:
        parts.append(tail)
    return "；".join(parts)


def _run_memory_extraction(scene: str):
    """按钮动作：按游标取一段新消息去提取，成功就把游标推进。"""
    orch = get_turn_graph()
    if not orch:
        return
    user_id = st.session_state["user_id"]
    session_id = st.session_state["browser_session_id"]
    thread_id = _current_thread_id()
    with st.spinner("正在提取记忆…"):
        window = orch.memory_extraction_window(user_id, session_id, scene)
        if not window.get("messages"):
            _clear_memory_extraction_state()
            _set_memory_extract_info(scene, "no_new", note="没有新的用户消息", thread_id=thread_id)
            return
        orch.start_memory_extraction(
            user_id,
            session_id,
            scene,
            messages=window["messages"],
        )
        payload = orch.wait_for_memory_extraction(
            user_id, session_id, scene, timeout=MEMORY_WAIT_SECONDS
        )
        payload = orch.pop_memory_extraction_result(user_id, session_id, scene) or payload

    if payload is None:
        _set_memory_extract_info(scene, "timeout", note="提取超时", thread_id=thread_id)
        return

    payload["messages_used"] = len(window["messages"])
    payload["remaining"] = int(window.get("remaining") or 0)
    _apply_memory_extraction_payload(
        orch, user_id, session_id, scene, payload, thread_id=thread_id
    )
    if not payload.get("has_error") and window.get("last_message_id"):
        # 失败不推进游标：这些消息还能重试，不能白丢。
        orch.record_memory_extraction(thread_id, window["last_message_id"])


def render_clarify_card() -> None:
    """追问卡片：每题 A/B/C 可直接点，D 自己填；一次可以问多个。"""
    pending = st.session_state.get("pending_clarify") or {}
    questions = pending.get("questions") or []
    if not questions or st.session_state.get("pending_assistant"):
        return

    from app.services.clarify_questions import compose_answer

    with st.container(key="clarify_card"):
        st.markdown(
            '<div class="sl-ask-head">'
            '<span class="sl-ask-kicker">还差几个信息</span>'
            '<span class="sl-ask-tip">点一个，或自己填</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        # 不用 st.form：表单里的控件在点提交前不会触发重跑，"D 自己填"选完
        # 也不会出现输入框。拆成普通控件后，选中即重跑，输入框当场出现。
        answers = []
        for question in questions:
            key = str(question.get("key") or "")
            labels = [option["label"] for option in question.get("options") or []]
            st.markdown(
                f'<div class="sl-ask-q">{_esc(question.get("title") or "")}</div>',
                unsafe_allow_html=True,
            )
            choice = ""
            if labels:
                choice = st.radio(
                    question.get("title") or key,
                    labels + [CUSTOM_OPTION_LABEL],
                    index=None,
                    key=f"clarify_{key}",
                    label_visibility="collapsed",
                    horizontal=True,
                )
            value = ""
            if not labels or choice == CUSTOM_OPTION_LABEL:
                value = st.text_input(
                    "自己填",
                    key=f"clarify_custom_{key}",
                    label_visibility="collapsed",
                    placeholder="例如：3 天 / 每周二出发",
                )
            elif choice:
                value = choice
            answers.append({
                "prefix": question.get("answer_prefix") or "",
                "value": value,
            })
        submitted = st.button("就这样，继续", key="clarify_submit", use_container_width=True)
        if submitted:
            text = compose_answer(answers)
            if text:
                st.session_state["pending_assistant"] = text
                st.rerun()


@st.cache_resource(show_spinner=False)
def _long_term_memory():
    """Chroma + embedding 初始化很贵；每页重建一次就会卡好几秒。"""
    from app.memory.long_term import LongTermMemory

    return LongTermMemory()


@st.cache_resource(show_spinner=False)
def _md_memory():
    from app.memory.md_memory import MDMemory

    return MDMemory()


def render_memory_entry(scene: str, key_suffix: str):
    """对话区的记忆入口：只有一个按钮，点了才提取，平时不跑也不花 token。"""
    thread_id = _current_thread_id()
    _, pending = _memory_progress(thread_id)
    if not st.session_state.get("assistant_msgs") and not pending:
        # 还没开始对话：不摆这个按钮。
        return
    with st.container(key="memory_entry"):
        button_col, hint_col = st.columns([1, 3.4])
        with button_col:
            clicked = st.button(
                "◇ 提取记忆",
                key=f"extract_memory_{key_suffix}",
                use_container_width=True,
                help="只提取上次之后的新消息；已经提取过的不会再送进模型",
            )
        with hint_col:
            st.markdown(
                '<div class="sl-memory-hint">'
                '<span class="sl-memory-hint-kicker">Memory</span>'
                f'<span>{_esc(_memory_entry_hint(thread_id))}</span></div>',
                unsafe_allow_html=True,
            )
    if clicked:
        _run_memory_extraction(scene)
        st.rerun()


def _apply_memory_extraction_payload(orch, user_id, session_id, scene, payload, *, thread_id=""):
    """把提取结果落到界面上：有候选就出候选卡片，没有就只更新那行说明。"""
    candidates = list(payload.get("candidates", []))
    diagnostic = payload.get("diagnostic", "")
    has_error = bool(payload.get("has_error", payload.get("status") == "error"))
    # 诊断只给失败用；正常结果那行说明由游标实时算，不铺模型原始输出。
    note = diagnostic if has_error else ""

    if not candidates and not has_error:
        _clear_memory_extraction_state()
        _set_memory_extract_info(scene, "no_content", note=note, thread_id=thread_id)
        return

    st.session_state["candidate_memories"] = candidates
    st.session_state["memory_saved_map"] = {}
    st.session_state["memory_diag"] = diagnostic if has_error else ""
    st.session_state["memory_extraction_error"] = has_error
    st.session_state["show_memory_extraction"] = True
    st.session_state["memory_extraction_scene"] = scene
    st.session_state["memory_extraction_thread"] = thread_id

    if candidates:
        _set_memory_extract_info(scene, "candidates", count=len(candidates), note=note)
    else:
        _set_memory_extract_info(scene, "error", note=diagnostic, thread_id=thread_id)


def _memory_saved_map() -> dict:
    return dict(st.session_state.get("memory_saved_map") or {})


def _write_memory_candidates(indices) -> int:
    """把指定候选写进长期记忆，返回写入条数。"""
    orch = get_turn_graph()
    candidates = list(st.session_state.get("candidate_memories") or [])
    if not orch or not candidates:
        return 0
    saved = _memory_saved_map()
    written = 0
    with st.spinner("正在写入长期记忆…"):
        for index in indices:
            if not 0 <= index < len(candidates) or index in saved:
                continue
            try:
                ids = orch.save_memory_candidates(
                    st.session_state["user_id"], [candidates[index]], [0]
                )
            except Exception as exc:
                log_exception("ui.memory_write", exc)
                st.error(f"这条没写进去：{type(exc).__name__}: {exc}")
                continue
            if ids:
                saved[index] = ids[0]
                written += 1
    st.session_state["memory_saved_map"] = saved
    return written


def _delete_memory_candidates(indices) -> int:
    """撤销写入：删掉刚才存进去的那条，行回到"未写入"。"""
    orch = get_turn_graph()
    saved = _memory_saved_map()
    removed = 0
    for index in list(indices):
        memory_id = saved.get(index)
        if not memory_id or not orch:
            continue
        try:
            ok = orch.delete_memory(st.session_state["user_id"], memory_id)
        except Exception as exc:
            log_exception("ui.memory_delete", exc)
            st.error(f"撤销失败：{type(exc).__name__}: {exc}")
            continue
        if ok:
            saved.pop(index, None)
            removed += 1
    st.session_state["memory_saved_map"] = saved
    return removed


MEMORY_CATEGORY_LABEL = {"shopping": "购物", "travel": "旅行", "general": "其他"}
MEMORY_CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}


def _memory_row_html(index: int, candidate: dict, saved: bool) -> str:
    category = MEMORY_CATEGORY_LABEL.get(candidate.get("category", "general"), "其他")
    confidence = MEMORY_CONFIDENCE_LABEL.get(candidate.get("confidence", "medium"), "中")
    meta = f"{category} · 置信度{confidence}"
    if saved:
        meta += " · 已写入长期记忆"
    return (
        '<div class="sl-memory-row-body">'
        f'<div class="sl-memory-row-text"><span class="sl-memory-row-no">{index + 1:02d}</span>'
        f'{_esc(candidate.get("content", ""))}</div>'
        f'<div class="sl-memory-row-meta">{_esc(meta)}</div>'
        "</div>"
    )


def render_memory_extraction_panel(scene, key_suffix):
    """候选记忆卡片：每行一个写入/撤销按钮，默认一条都不写入。"""
    if not st.session_state.get("show_memory_extraction"):
        return
    if scene and st.session_state.get("memory_extraction_scene") != scene:
        return
    owner = st.session_state.get("memory_extraction_thread") or ""
    if owner and owner != _current_thread_id():
        # 候选属于另一个会话：切回来再看，不在这里串场。
        return

    has_error = bool(st.session_state.get("memory_extraction_error"))
    candidates = st.session_state.get("candidate_memories", [])
    if not candidates and not has_error:
        return

    with st.container(key="memory_panel"):
        if has_error:
            st.markdown(
                '<div class="sl-memory-head">'
                '<div class="sl-card-kicker">Memory</div>'
                "<h3>这次提取没成功</h3>"
                "<p>正文已经生成，不影响这轮回答；可以再点一次「提取记忆」。</p>"
                "</div>",
                unsafe_allow_html=True,
            )
            diagnostic = st.session_state.get("memory_diag", "")
            if diagnostic:
                with st.expander("失败诊断"):
                    st.code(diagnostic)
            if st.button("知道了", key=f"dismiss_memory_error_{key_suffix}"):
                _finish_memory_extract("上次提取失败，已忽略")
                _clear_memory_extraction_state()
                st.rerun()
            return

        saved = _memory_saved_map()
        all_saved = bool(candidates) and len(saved) >= len(candidates)
        st.markdown(
            '<div class="sl-memory-head">'
            '<div class="sl-card-kicker">Memory candidates</div>'
            f"<h3>这次提取到 {len(candidates)} 条</h3>"
            "<p>默认一条都不写入。点左边的「写入」才会存进长期记忆，再点一次就是撤销并删除。</p>"
            "</div>",
            unsafe_allow_html=True,
        )

        for index, candidate in enumerate(candidates):
            is_saved = index in saved
            with st.container(key=f"memory_row_{key_suffix}_{index}_{'written' if is_saved else 'fresh'}"):
                button_col, body_col = st.columns([1, 3.4])
                with button_col:
                    label = "已写入 · 撤销" if is_saved else "写入"
                    if st.button(
                        label,
                        key=f"memory_row_btn_{key_suffix}_{index}",
                        use_container_width=True,
                        type="primary" if is_saved else "secondary",
                        help="再点一次会删掉这条记忆，回到未写入" if is_saved else "写进长期记忆（MD + 向量）",
                    ):
                        if is_saved:
                            _delete_memory_candidates([index])
                        else:
                            _write_memory_candidates([index])
                        st.rerun()
                with body_col:
                    st.markdown(
                        _memory_row_html(index, candidate, is_saved),
                        unsafe_allow_html=True,
                    )

        action_col, fold_col = st.columns([1.1, 1.1])
        with action_col:
            label = "全部撤销" if all_saved else "全部写入"
            if st.button(label, key=f"memory_all_{key_suffix}", use_container_width=True):
                if all_saved:
                    _delete_memory_candidates(range(len(candidates)))
                else:
                    written = _write_memory_candidates(range(len(candidates)))
                    _finish_memory_extract(f"已写入 {written} 条")
                st.rerun()
        with fold_col:
            if st.button("收起", key=f"fold_memories_{key_suffix}", use_container_width=True):
                written = len(_memory_saved_map())
                _finish_memory_extract(f"已写入 {written} 条" if written else "这次没有写入")
                _clear_memory_extraction_state()
                st.rerun()


# ========== 实时流式渲染器 ==========
def run_streaming_realtime(async_gen_factory, *, postprocess=None):
    """实时渲染 streaming 事件流。

    ``postprocess`` 在正文流结束后、同一助手气泡刷新前执行，可返回追加到步骤区的
    后处理事件。这样 Step 4 会更新到固定的过程区，而不会落到正文下方。
    """
    try:
        return _run_streaming_impl(
            async_gen_factory,
            postprocess=postprocess,
        )
    except Exception as e:
        log_exception("ui.streaming", e)
        st.error(f"流式处理失败: {e}")
        return "", []


def _esc(s):
    """转义动态文本，安全地嵌入样式卡片 HTML。"""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_execution_logs(events):
    logs = []
    regular = []
    for event in events or []:
        if event.get("event") == "execution_log":
            logs.append(event)
        else:
            regular.append(event)
    return regular, logs


ROUTINE_LOG_KINDS = {"node_started", "node_succeeded"}


def _execution_logs_html(events):
    """只保留分支、重试、降级、异常这类有信息量的日志，节点起停默认收进折叠区。"""
    _, logs = _split_execution_logs(events)
    logs = [
        event for event in logs
        if not (
            (event.get("data") or {}).get("kind") in ROUTINE_LOG_KINDS
            and (event.get("data") or {}).get("status") in {"ok", "running", None}
        )
    ]
    if not logs:
        return ""
    rows = []
    for index, event in enumerate(logs, start=1):
        data = event.get("data", {}) or {}
        status = data.get("status", "info")
        icon = {"ok": "✓", "warning": "!", "error": "×", "running": "…"}.get(status, "·")
        graph = _esc(data.get("graph", ""))
        node = _esc(data.get("node", ""))
        kind = _esc(data.get("kind", ""))
        message = _esc(data.get("message", ""))
        branch = data.get("branch")
        attempt = data.get("attempt")
        max_attempts = data.get("max_attempts")
        iteration = data.get("iteration")
        max_iterations = data.get("max_iterations")
        duration = data.get("duration_ms")
        source_timings = (data.get("details") or {}).get("source_timings_ms") or {}
        meta = [f"<code>{graph}:{node}</code>", kind]
        if branch:
            meta.append(f"分支 <code>{_esc(branch)}</code>")
        if attempt is not None:
            meta.append(f"尝试 {_esc(attempt)}/{_esc(max_attempts or '?')}")
        if iteration is not None:
            meta.append(f"迭代 {_esc(iteration)}/{_esc(max_iterations or '?')}")
        if duration is not None:
            meta.append(f"{_esc(duration)}ms")
        if source_timings:
            timing_text = " · ".join(
                f"{_esc(name)} {_esc(value)}ms"
                for name, value in source_timings.items()
            )
            meta.append(f"数据源 {timing_text}")
        rows.append(
            f'<div class="execution-log-row execution-log-{_esc(status)}">'
            f'<span class="execution-log-icon">{icon}</span>'
            f'<div><div class="execution-log-meta">#{index} · {" · ".join(meta)}</div>'
            f'<div class="execution-log-message">{message}</div></div></div>'
        )
    return (
        '<details class="execution-log-panel">'
        f'<summary>执行日志 · {len(logs)} 条（分支、重试、循环、降级）</summary>'
        f'<div class="execution-log-list">{"".join(rows)}</div>'
        '</details>'
    )


def _timing_summary_html(events):
    """展示用户可感知延迟与完整链路延迟，避免只看到单个节点耗时。"""
    done_timing = {}
    pipeline_timing = {}
    for event in events or []:
        data = event.get("data") or {}
        if event.get("event") == "done":
            done_timing = data.get("timing_ms") or {}
        elif event.get("event") == "memory_extraction":
            pipeline_timing = data.get("timing_ms") or {}

    response_ready = done_timing.get("response_ready_ms")
    if response_ready is None:
        return ""
    first_token = done_timing.get("time_to_first_token_ms")
    post_response = pipeline_timing.get("post_response_ms")
    pipeline = pipeline_timing.get("pipeline_ms") or done_timing.get("pipeline_ms")
    metrics = [
        ("首 Token", first_token),
        ("回答完成", response_ready),
        ("回答后处理", post_response),
        ("完整链路", pipeline),
    ]
    rows = " · ".join(
        f"{_esc(label)} {_esc(value)}ms"
        for label, value in metrics
        if isinstance(value, (int, float))
    )
    return (
        '<div class="execution-log-row execution-log-ok">'
        '<span class="execution-log-icon">⏱</span>'
        f'<div><div class="execution-log-meta">链路耗时</div>'
        f'<div class="execution-log-message">{rows}</div></div></div>'
    )


def _fmt_event_html(evt):
    """把单个过程事件渲染成统一的 HTML 过程卡片。"""
    et = evt.get("event", "")
    data = evt.get("data", {}) or {}
    if et == "thinking":
        return ""
    if et == "classify":
        route = data.get("route", "") or "—"
        intent = data.get("intent", "") or "—"
        sub = data.get("sub_intent", "") or "—"
        retr = data.get("retrieval", "") or "—"
        conf = data.get("confidence", "") or "—"
        reason = data.get("reason", data.get("reasoning", "")) or "—"
        method = data.get("method", "") or "—"
        return (
            '<div class="process-card completed">'
            '<div class="process-title">已识别任务意图</div>'
            f'<div class="process-copy">路由 {_esc(route)} · {_esc(intent)} · {_esc(sub)}<br>'
            f'检索 {_esc(retr)} · 置信度 {_esc(conf)} · {_esc(method)}<br>'
            f'{_esc(reason)}</div></div>'
        )
    if et == "step":
        step_num = data.get("step", 0)
        total = data.get("total", 0)
        name = data.get("name", "")
        desc = data.get("description", "")
        source = data.get("source", "")
        status = data.get("status", "active")
        source_label = f" · 来源 {_esc(source)}" if source else ""
        return (
            f'<div class="process-card {_esc(status)}">'
            f'<div class="process-title">{step_num}/{total} · {_esc(name)}</div>'
            f'<div class="process-copy">{_esc(desc)}{source_label}</div></div>'
        )
    if et == "tool_call":
        tn = data.get("tool", "")
        if tn in {"行程执行", "计划自检", "计划审核"}:
            return ""
        ti = (data.get("input", "") or "").replace("\n", " ").strip()
        is_completed = data.get("status") == "completed"
        status = "completed" if is_completed else "active"
        tool_output = (data.get("output", "") or "").replace("\n", " ").strip()
        if tn == "nl2sql":
            short_sql = ti if len(ti) <= 160 else ti[:160] + "…"
            action = "结构化查询已完成" if is_completed else "正在执行结构化查询"
            return (
                f'<div class="process-card {status}">'
                '<div class="process-title">商品数据检索</div>'
                f'<div class="process-copy">{action}<br><code>{_esc(short_sql)}</code>'
                f'{f"<br>{_esc(tool_output)}" if tool_output else ""}</div></div>'
            )
        if tn == "rag":
            action = "检索已完成" if is_completed else "正在检索"
            return (
                f'<div class="process-card {status}">'
                '<div class="process-title">评价与攻略检索</div>'
                f'<div class="process-copy">{action} · {_esc(ti)}'
                f'{f"<br>{_esc(tool_output)}" if tool_output else ""}</div></div>'
            )
        return (
            f'<div class="process-card {status}">'
            f'<div class="process-title">{_esc(tn)}{" · 已完成" if is_completed else ""}</div>'
            f'<div class="process-copy">{_esc(ti)}'
            f'{f"<br>{_esc(tool_output)}" if tool_output else ""}</div></div>'
        )
    if et == "tool_result":
        tn = data.get("tool", "")
        if tn in {"行程执行", "计划自检", "计划审核"}:
            return ""
        output = (data.get("output", "") or "").replace("\n", " ").strip()
        return (
            '<div class="process-card completed">'
            f'<div class="process-title">{_esc(tn)} · 已完成</div>'
            f'<div class="process-copy">{_esc(output)}</div></div>'
        )
    if et == "error":
        return (
            '<div class="process-card error">'
            '<div class="process-title">处理失败</div>'
            f'<div class="process-copy">{_esc(data.get("message", ""))}</div></div>'
        )
    return ""


def _persistent_events(events):
    """running/thinking 是瞬态状态，不进入已生成消息的永久过程记录。"""
    return deduplicate_process_events([
        evt for evt in events
        if evt.get("event") not in {
            "thinking", "token", "response_reset", "done", "memory_extraction"
        }
    ])


def _process_events_html(events, include_transient=False, active_step=None, collapsed=False):
    """生成一组过程事件的稳定 HTML，按逻辑节点去重。"""
    normalized_active_step = active_step or infer_active_step(events)
    persistent = events if include_transient else _persistent_events(events)
    regular_events, _ = _split_execution_logs(persistent)
    source_events = build_process_timeline(
        reconcile_tool_events(
            deduplicate_process_events(regular_events),
            completed=normalized_active_step >= 4,
        ),
        active_step=normalized_active_step,
    )
    cards = []
    for evt in source_events:
        h = _fmt_event_html(evt)
        if h:
            cards.append(h)
    body = "".join(cards) + _timing_summary_html(events) + _execution_logs_html(persistent)
    if collapsed and cards:
        # 已完成的轮次默认收起：正文优先，过程留可展开的证据。
        return (
            f'<details class="process-panel"><summary>运行过程 · {len(cards)} 条</summary>'
            f"{body}</details>"
        )
    return body


def render_process_events(events, include_transient=False, collapsed=False):
    """把一组过程事件按顺序渲染成样式卡片（步骤在前、与正文区分）。"""
    cards = _process_events_html(
        events,
        include_transient=include_transient,
        active_step=4,
        collapsed=collapsed,
    )
    if cards:
        st.markdown(cards, unsafe_allow_html=True)


def _plan_payload(events):
    """取本轮最后一份结构化方案（trip_gear 的 plan_result 事件）。"""
    for evt in reversed(events or []):
        if evt.get("event") == "plan_result":
            data = evt.get("data")
            if isinstance(data, dict):
                return data
    return None


def _plan_lead_html(payload):
    facts = payload.get("facts") or {}
    days = len((payload.get("itinerary") or {}).get("days") or [])
    gear = payload.get("gear") or []
    constraints = payload.get("constraints") or []
    issues = payload.get("issues") or []
    parts = []
    if days:
        parts.append(f"{days} 天")
    if facts.get("people"):
        parts.append(f"{facts.get('people')} 人")
    elif facts:
        # 人数没说：显示"未指定"，不要静默当成 1 人
        parts.append("人数未指定")
    if facts.get("destination"):
        parts.append(str(facts["destination"]))
    head = " · ".join(parts) if parts else "方案"
    tail = f"约束 {len(constraints)} 条 · 装备 {len(gear)} 项"
    if issues:
        tail += f" · {len(issues)} 处未满足"
    return (
        '<div class="sl-lead">方案已生成：'
        f'<em>{head}</em>，{tail}。'
        '<span>逐项可回溯到约束来源与工具结果。</span></div>'
    )


def render_assistant_message(msg):
    """渲染一条助手消息：结构化面板优先，Markdown 原文收进折叠区。"""
    if isinstance(msg.get("process_events"), list) or "response" in msg:
        events = msg.get("process_events", [])
        render_process_events(events, include_transient=False, collapsed=True)
        payload = _plan_payload(events)
        resp = msg.get("response", "")
        if payload:
            st.markdown(_plan_lead_html(payload), unsafe_allow_html=True)
            st.markdown(plan_panels_html(payload), unsafe_allow_html=True)
            if resp:
                with st.expander("查看 Markdown 原文"):
                    st.markdown(resp)
        elif resp:
            st.markdown(resp)
    else:
        st.markdown(msg.get("content", ""))


def _render_process_panel(panel, events, include_transient=False, active_step=None):
    """整块替换固定过程区，避免旧 DOM 和重复步骤被追加显示。"""
    html = _process_events_html(
        events,
        include_transient=include_transient,
        active_step=active_step,
    )
    if html:
        panel.markdown(html, unsafe_allow_html=True)
    else:
        panel.empty()


def _is_revision_signal(evt):
    return (
        evt.get("event") == "response_reset"
        and not (evt.get("data") or {}).get("section")
    )


def _run_streaming_impl(async_gen_factory, *, postprocess=None):
    """后台消费 SSE，主 UI 在两个固定容器内分别更新步骤和正文。"""
    event_q = thread_queue.Queue()
    all_events = []
    response_buffer = StreamResponseBuffer()
    stop_event = threading.Event()

    def _bg_worker():
        async def _drain():
            try:
                gen = async_gen_factory()
                try:
                    async for sse_line in gen:
                        if stop_event.is_set():
                            break
                        if sse_line.startswith("data: "):
                            try:
                                event_q.put(json.loads(sse_line[6:]))
                            except json.JSONDecodeError:
                                pass
                finally:
                    await gen.aclose()
                event_q.put(None)
            except Exception as e:
                log_exception("ui.streaming_worker", e)
                event_q.put({"event": "error", "data": {"message": str(e)}, "step": ""})
                event_q.put(None)

        try:
            SharedAsyncRunner.submit(_drain()).result()
        except Exception as e:
            log_exception("ui.streaming_worker_join", e)
            event_q.put({"event": "error", "data": {"message": str(e)}, "step": ""})
            event_q.put(None)

    bg = threading.Thread(target=_bg_worker, daemon=True)
    bg.start()

    loading = st.empty()
    loading.markdown("⏳ 正在思考，正在规划处理步骤…")

    # 单一 empty 占位符每次整块替换，避免流式刷新累积旧 DOM。
    process_panel = st.empty()
    response_container = st.empty()
    first_event = True
    rendered_active_step = 1
    last_response_render = 0.0
    _render_process_panel(
        process_panel,
        [],
        include_transient=True,
        active_step=1,
    )

    try:
        while True:
            try:
                evt = event_q.get(timeout=0.05)
            except thread_queue.Empty:
                if not bg.is_alive():
                    break
                continue

            if evt is None:
                break

            all_events.append(evt)
            event_type = evt.get("event", "")
            data = evt.get("data", {}) or {}

            if first_event:
                loading.empty()
                first_event = False

            if _is_revision_signal(evt):
                response_container.empty()

            response_buffer.handle_event(evt)

            if event_type == "token":
                now = time.monotonic()
                if now - last_response_render >= 0.05:
                    response_container.markdown(response_buffer.text)
                    last_response_render = now
                current_active_step = infer_active_step(all_events)
                if current_active_step != rendered_active_step:
                    _render_process_panel(
                        process_panel,
                        all_events,
                        include_transient=True,
                        active_step=current_active_step,
                    )
                    rendered_active_step = current_active_step
                continue

            if event_type == "response_reset":
                if response_buffer.text:
                    response_container.markdown(response_buffer.text)
                else:
                    response_container.empty()
                last_response_render = 0.0
                continue

            if event_type == "done":
                if response_buffer.text:
                    response_container.markdown(response_buffer.text)
                    last_response_render = time.monotonic()
                continue

            current_active_step = infer_active_step(all_events)
            _render_process_panel(
                process_panel,
                all_events,
                include_transient=True,
                active_step=current_active_step,
            )
            rendered_active_step = current_active_step
    finally:
        stop_event.set()

    if first_event:
        loading.empty()

    final_response = response_buffer.text
    _render_process_panel(
        process_panel,
        all_events,
        include_transient=False,
        active_step=4,
    )
    if postprocess:
        postprocess_events = postprocess(final_response, list(all_events)) or []
        all_events.extend(postprocess_events)
        all_events = deduplicate_process_events(all_events)

    # 完成后移除“正在生成…”等瞬态状态，只保留有永久价值的过程事件。
    _render_process_panel(
        process_panel,
        all_events,
        include_transient=False,
        active_step=4,
    )
    return final_response, all_events


# ========== 导航与页面组件 ==========
PAGE_BY_LABEL = {label: key for _icon, label, key in NAV_ITEMS}
# 目录一页渲染多少件：太多会卡，太少翻页累
CATALOG_PAGE_SIZE = 24


def render_option_group(values, *, key, labels=None, default=None, columns=None):
    """用整块按钮卡片表达选择状态，不显示 Radio 圆点。"""
    values = list(values)
    labels = list(labels or values)
    default_value = values[0] if default is None else default
    selected = st.session_state.get(key, default_value)
    if selected not in values:
        selected = values[0]
    column_count = columns or len(values)
    cols = st.columns(column_count)
    for index, (value, label) in enumerate(zip(values, labels)):
        with cols[index % column_count]:
            if st.button(
                label,
                key=f"{key}_option_{index}",
                use_container_width=True,
                type="primary" if selected == value else "secondary",
            ):
                st.session_state[key] = value
                st.rerun()
    return selected


def _current_thread_id() -> str:
    """左栏高亮用：当前浏览器会话对应的对话 thread。"""
    from app.session import build_thread_id

    return build_thread_id(
        st.session_state["user_id"],
        "general",
        st.session_state["browser_session_id"],
    )


THREAD_TITLE_CHARS = 24
# 最近会话全量显示，列表自己滚，不再折叠
CUSTOM_OPTION_LABEL = "D · 自己填"


def _clip_thread_title(text: str, limit: int = THREAD_TITLE_CHARS) -> str:
    """会话标题只做安全截断，显示宽度交给 CSS 的省略号。

    这里截太狠会让不同的会话显示成同一个标题（"去苏州玩两天，一万…" 出现三遍），
    看起来像历史被改过。
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _recent_threads(limit: int | None = 4):
    """左栏「最近会话」：按最近写入排序，每条都可由侧栏按钮点进去。

    ``limit=None`` 表示取全部，供"展开全部"用。
    """
    try:
        from app.memory.conversation_store import ConversationStore
        rows = ConversationStore().list_threads(user_id=st.session_state["user_id"])
    except Exception:
        return []
    output = []
    for row in rows:
        preview = str(row.get("preview") or "").strip()
        if not preview or not int(row.get("message_count") or 0):
            continue
        if limit is not None and len(output) >= limit:
            break
        updated_at = str(row.get("last_at") or "")[:16].replace("T", " ")
        output.append({
            "thread_id": row.get("thread_id", ""),
            "title": _clip_thread_title(preview),
            "full_title": preview,
            "updated_at": updated_at,
            # 同一句话问过好几次时，只有时间能把它们分开。
            "time": updated_at[11:16],
        })
    return output


def _render_delete_confirm(thread) -> None:
    """会话行的删除确认：就地长在那一行下面，删完/取消即收。"""
    with st.container(key="thread_delete_confirm"):
        st.markdown(
            f'<div class="sl-confirm-line">删除「'
            f'{_esc(thread.get("full_title") or thread.get("title") or "")}」？删了就找不回来。</div>',
            unsafe_allow_html=True,
        )
        confirm_col, cancel_col = st.columns(2, gap="small")
        if confirm_col.button(
            "删除", key="confirm_delete_thread", type="primary", use_container_width=True
        ):
            from app.memory.conversation_store import ConversationStore

            thread_id = str(thread.get("thread_id") or "")
            ConversationStore().delete_thread(thread_id)
            current = st.session_state.get("active_thread_id") or _current_thread_id()
            if thread_id == str(current):
                _start_new_thread()
            st.session_state.pop("pending_thread_delete", None)
            st.rerun()
        if cancel_col.button("取消", key="cancel_delete_thread", use_container_width=True):
            st.session_state.pop("pending_thread_delete", None)
            st.rerun()


def _thread_tip_html(thread) -> str:
    """会话行的悬停详情：画在侧栏右边，不盖住上面的会话。

    Streamlit 原生 ``help`` tooltip 位置写死在上方（前端 ``placement || TOP``），
    Python 层没有参数可调；这里用 ``position: fixed`` + 自动 top 贴着这一行往右摆。
    """
    title = str(thread.get("full_title") or thread.get("title") or "").strip()
    updated = str(thread.get("updated_at") or thread.get("last_at") or "")[:16]
    meta = f"最后更新 {updated}" if updated else ""
    return (
        '<div class="sl-thread-tip">'
        f'<b>{_esc(title)}</b>'
        f'<span>{_esc(meta)}</span>'
        "</div>"
    )


def _start_new_thread() -> None:
    """新建对话：换一个浏览器会话 id，等于开一个全新的 thread。"""
    st.session_state["browser_session_id"] = uuid.uuid4().hex
    st.session_state["active_thread_id"] = ""
    st.session_state["assistant_msgs"] = []
    st.session_state["messages"] = []
    # 旧会话没走完的交互状态不能带进新会话。
    for stale_key in ("pending_assistant", "pending_clarify", "pending_compressed_summary",
                      "last_memory_extract"):
        st.session_state.pop(stale_key, None)


MAX_TURN_EVENT_BYTES = 256 * 1024


def _turn_events_for_storage(events):
    """历史回看只需要能撑起面板的事件；超量时退化成只留方案事件。"""
    if not events:
        return []
    try:
        if len(json.dumps(events, ensure_ascii=False)) <= MAX_TURN_EVENT_BYTES:
            return list(events)
    except (TypeError, ValueError):
        pass
    return [dict(evt) for evt in events if evt.get("event") == "plan_result"]


def _annotate_last_assistant(thread_id: str, metadata: dict) -> bool:
    """把元数据补进该 thread 最后一条助手消息。

    Streamlit 热重载不会重载被 import 的模块，``ConversationStore`` 可能还是旧版
    （没有 ``annotate_last_assistant``），取不到方法时直接写库兜底。
    """
    from app.memory.conversation_store import ConversationStore

    store = ConversationStore()
    method = getattr(store, "annotate_last_assistant", None)
    if method is not None:
        return bool(method(thread_id, metadata))

    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT id, metadata_json FROM conversation_messages "
            "WHERE thread_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            merged = json.loads(row[1] or "{}")
        except json.JSONDecodeError:
            merged = {}
        merged.update(metadata)
        conn.execute(
            "UPDATE conversation_messages SET metadata_json = ? WHERE id = ?",
            (json.dumps(merged, ensure_ascii=False, sort_keys=True), row[0]),
        )
        conn.commit()
    return True


def _persist_clarify_question(thread_id: str, text: str) -> None:
    """追问轮没有 finalize，助手那条提问得由界面补进会话库。

    补一次就够：同一条问题已经是最新助手消息时直接跳过，重跑不会写第二份。
    """
    if not thread_id or not text.strip():
        return
    try:
        from app.memory.conversation_store import ConversationStore

        store = ConversationStore()
        rows = store.list_messages(thread_id, last_n=1)
        if rows and rows[-1].get("content") == text:
            return
        store.append_message(thread_id, "assistant", text)
    except Exception as exc:
        log_exception("ui.persist_clarify_question", exc)


def _persist_turn_events(events) -> None:
    """把本轮事件补进助手消息：切回该会话时结构化面板才在。"""
    if not events:
        return
    try:
        ok = _annotate_last_assistant(
            _current_thread_id(),
            {"process_events": _turn_events_for_storage(events)},
        )
        if ok:
            st.session_state.pop("turn_persist_error", None)
        else:
            st.session_state["turn_persist_error"] = "没有找到本轮助手消息，事件未入库"
    except Exception as exc:
        st.session_state["turn_persist_error"] = f"{type(exc).__name__}: {exc}"
        log_exception("ui.persist_turn_events", exc)


def _activate_thread(thread_id: str) -> None:
    """点左栏会话：让浏览器会话对齐到该 thread，并把历史消息载回对话区。

    会话身份由 ``build_thread_id(user, scene, session_id)`` 组成，所以这里只改
    ``browser_session_id``：之后的新消息自然续写同一个 thread，而不用另建会话。
    """
    from app.memory.conversation_store import ConversationStore

    if not thread_id:
        return
    store = ConversationStore()
    session_id = ConversationStore.split_thread_id(thread_id).get("session_id") or ""
    if not session_id:
        return

    restored = []
    for row in store.list_messages(thread_id):
        role = str(row.get("role") or "")
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            restored.append({"role": "user", "content": content})
        elif role == "assistant":
            events = (row.get("metadata") or {}).get("process_events")
            restored.append({
                "role": "assistant",
                "response": content,
                "process_events": events if isinstance(events, list) else [],
            })

    st.session_state["browser_session_id"] = session_id
    st.session_state["active_thread_id"] = thread_id
    st.session_state["assistant_msgs"] = restored
    # 换会话就丢弃上一轮没走完的交互状态，避免串到别的 thread 上。
    for stale_key in ("pending_assistant", "pending_clarify", "pending_compressed_summary",
                      "last_memory_extract"):
        st.session_state.pop(stale_key, None)


def render_app_sidebar():
    with st.sidebar:
        st.markdown(brand_html(), unsafe_allow_html=True)

        # 品牌之下、导航之上：新建对话 + 记忆提示，都是"这一屏"的动作，不进导航。
        with st.container(key="new_thread"):
            if st.button(
                "＋ 新建对话",
                key="new_thread_side",
                use_container_width=True,
                # 和工作台里"选中的那一项"同一个按钮类型，样式自然一致
                type="primary",
                help="开一个全新的会话，当前会话留在「最近会话」里",
            ):
                _start_new_thread()
                st.rerun()

        pending_count = _pending_confirm_count()
        if pending_count:
            st.markdown(
                alert_html(
                    "warn",
                    f"{pending_count} 项待确认",
                    "候选记忆在对话区就能选，敏感操作到记忆中心。",
                ),
                unsafe_allow_html=True,
            )

        st.markdown(nav_label_html("工作台"), unsafe_allow_html=True)
        current = st.session_state.get("active_page", "assistant")
        with st.container(key="nav_menu"):
            for _icon, label, page_key in NAV_ITEMS:
                if st.button(
                    label,
                    key=f"nav_{page_key}",
                    use_container_width=True,
                    type="primary" if current == page_key else "secondary",
                ):
                    st.session_state["active_page"] = page_key
                    st.rerun()

        threads = _recent_threads(limit=None)
        active_thread_id = st.session_state.get("active_thread_id") or _current_thread_id()
        st.markdown(nav_label_html("最近会话"), unsafe_allow_html=True)
        with st.container(key="recent_threads"):
            pending_delete = st.session_state.get("pending_thread_delete") or {}
            # 目标会话已经不在列表里（别处删掉了）：清掉待删状态，免得多出一个空确认
            if pending_delete and not any(
                str(thread["thread_id"]) == str(pending_delete.get("thread_id") or "")
                for thread in threads
            ):
                st.session_state.pop("pending_thread_delete", None)
                pending_delete = {}
            if threads:
                with st.container():
                    for index, thread in enumerate(threads):
                        is_active = thread["thread_id"] == active_thread_id
                        with st.container(key=f"recent_row_{index}"):
                            # 先摆详情再摆按钮：fixed 元素没有 top 时用静态位置，
                            # 放在行首才能让浮层和这一行对齐。
                            st.markdown(_thread_tip_html(thread), unsafe_allow_html=True)
                            title_col, delete_col = st.columns(
                                [0.86, 0.14], gap="small", vertical_alignment="center"
                            )
                            with title_col:
                                if st.button(
                                    f"{thread['time']}  {thread['title']}".strip(),
                                    key=f"recent_thread_{index}",
                                    use_container_width=True,
                                    # tertiary = 纯文字，没有边框和底色，天然不是卡片；
                                    # 当前会话用 primary 只为让 CSS 换成整行浅底+强调色。
                                    # 不用 help：原生 tooltip 只会弹在正上方，把上一行盖住；
                                    # 详情改由行内 .sl-thread-tip 画在侧栏右边。
                                    type="primary" if is_active else "tertiary",
                                ):
                                    _activate_thread(thread["thread_id"])
                                    st.rerun()
                            with delete_col:
                                if st.button("✕", key=f"recent_delete_{index}"):
                                    st.session_state["pending_thread_delete"] = {
                                        "thread_id": thread["thread_id"],
                                        "title": thread.get("full_title") or thread.get("title") or "",
                                    }
                                    st.rerun()
                            # 确认块就长在这一行下面：删哪条弹哪条，不跑到列表顶部
                            if str(pending_delete.get("thread_id") or "") == str(thread["thread_id"]):
                                _render_delete_confirm(thread)
            else:
                st.markdown(
                    '<div class="sl-thread"><div class="sl-thread-title">还没有会话</div>'
                    '<div class="sl-thread-meta">发出第一条需求试试</div></div>',
                    unsafe_allow_html=True,
                )

        return current


EMPTY_STATE_HTML = """
<div class="sl-empty sl-empty--left">
  <div class="sl-empty-title">从一句话开始</div>
  <p class="sl-empty-quote">「周末两个人去莫干山露营，预算 1500，我有帐篷和睡袋。」</p>
  <p>系统会派生约束清单 → 生成逐日行程 → 由行程推导装备并判定已有 → 只对缺口检索商品 → 校验预算并在有限轮次内修复。</p>
</div>
"""


def _pending_confirm_count():
    """侧栏提醒用：候选记忆 + 原位敏感操作。"""
    return int(bool(st.session_state.get("show_memory_extraction"))) + sum(
        1 for key, value in st.session_state.items()
        if key.startswith("pending_inline_") and value
    )


def render_assistant_page():
    render_page_header(
        "行程与装备协同",
        "说一句出行需求：系统派生约束清单、生成逐日行程、按行程推导装备，并在预算内完成校验与有限修复。",
    )

    if "assistant_msgs" not in st.session_state:
        st.session_state["assistant_msgs"] = []

    # 一句话已经在路上时，空态卡片立刻让位：否则这一轮会同时出现"卡片 + 我的消息"。
    pending_message = st.session_state.get("pending_assistant")
    with st.container():
        if not st.session_state["assistant_msgs"] and not pending_message:
            st.markdown(EMPTY_STATE_HTML, unsafe_allow_html=True)

        for msg in st.session_state["assistant_msgs"]:
            if msg.get("role") == "user":
                st.markdown(user_bubble_html(msg.get("content", "")), unsafe_allow_html=True)
            else:
                with st.chat_message("assistant"):
                    render_assistant_message(msg)

        # 追问还没答完时，这一屏只该有追问卡片：记忆入口和"事件未入库"这类
        # 内部提示都是噪声，而且追问轮本来就没有助手正文可挂事件。
        clarifying = bool(st.session_state.get("pending_clarify"))
        if not clarifying:
            if persist_error := st.session_state.get("turn_persist_error"):
                st.caption(f"本轮的步骤记录没有写进会话库，切回来只会看到正文：{persist_error}")

            # 记忆入口就在对话里：一个按需按钮 + 上次结果 + 候选清单，不必跳到记忆中心。
            render_memory_entry("general", "assistant")
            render_memory_extraction_panel("general", "assistant")
        render_clarify_card()

        if pending_message:
            st.session_state.pop("pending_assistant", None)
            st.session_state["assistant_msgs"].append({"role": "user", "content": pending_message})
            st.markdown(user_bubble_html(pending_message), unsafe_allow_html=True)
            with st.chat_message("assistant"):
                orch = get_turn_graph()
                uid = st.session_state["user_id"]
                browser_session_id = st.session_state["browser_session_id"]
                # 上一轮被"缺少核心信息"中断时，这一句是补充答案，要走恢复而不是新开一轮。
                resuming = st.session_state.pop("pending_clarify", None)
                if orch and resuming:
                    async def _gen():
                        async for line in orch.resume_streaming(
                            pending_message,
                            uid,
                            browser_session_id,
                            scene="general",
                        ):
                            yield line
                elif orch:
                    async def _gen():
                        async for line in orch.process_streaming(
                            pending_message,
                            uid,
                            browser_session_id,
                            scene="general",
                        ):
                            yield line

                clarify = None
                if orch:
                    # 记忆不再在回答后自动判断：提取由对话区的按钮按需触发。
                    response, events = run_streaming_realtime(_gen)
                    clarify = next(
                        (event for event in events if event.get("event") == "clarify"),
                        None,
                    )
                    if clarify:
                        data = clarify.get("data", {}) or {}
                        st.session_state["pending_clarify"] = {
                            "thread_id": data.get("thread_id", ""),
                            "missing": data.get("missing", []),
                            "questions": data.get("questions", []),
                        }
                        response = (
                            data.get("question")
                            or "还需要补充一些信息才能继续，请直接回复缺失的内容。"
                        )
                        # 追问轮被 interrupt 截住，finalize 不跑，图不会写这条助手
                        # 消息；旧消息迁移现在只补一次，不在这里补就会丢问题本身。
                        _persist_clarify_question(
                            str(data.get("thread_id") or ""), response
                        )
                else:
                    response = "请先在个人中心配置主模型后开始对话。"
                    events = []
                    st.warning(response)
            if clarify:
                # 追问轮没有助手正文入库，挂事件只会报"没有找到本轮助手消息"
                st.session_state.pop("turn_persist_error", None)
            else:
                _persist_turn_events(events)
            st.session_state["assistant_msgs"].append({
                "role": "assistant",
                "response": response,
                "process_events": events,
            })
            st.rerun()

    # 放进 st.bottom：输入框仍固定在底部，但 Streamlit 不再把首屏滚到最底
    # （只有"自动定位到底部"的 chat_input 才会触发整页滚到底）。
    with st.bottom:
        if user_input := st.chat_input(
            "描述购物、旅行或任何问题，系统会自动选择 Agent…",
            key="assistant_input",
        ):
            st.session_state["pending_assistant"] = user_input
            st.rerun()


def render_product_detail(product_id: int):
    product = load_product(product_id)
    if not product:
        render_empty("没有找到这个商品。")
        return
    render_section_header(product["name"], f"{product.get('brand', '')} · {product.get('category', '')} / {product.get('subcategory', '')}")
    left, right = st.columns([1.45, 1])
    with left:
        st.markdown(
            f"""
            <div class="ui-card">
              <div class="card-kicker">{product.get('brand') or '精选商品'}</div>
              <h3>{product['name']}</h3>
              <p>{product.get('description') or '暂无商品介绍'}</p>
              <div class="card-meta">
                <span class="pill">¥ {product['price']:.0f}</span>
                <span class="pill teal">评分 {product.get('community_rating') or product.get('rating') or 0:.1f}</span>
                <span class="pill amber">库存 {product.get('stock', 0)}</span>
                {'<span class="pill teal">防水</span>' if product.get('waterproof') else ''}
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with right:
        render_metric("社区评价", product.get("review_count", 0), "来自本地评价数据")
        st.caption("目录仅展示数据，不直接产生交易。")
        if st.button("← 返回生活目录", key="back_to_products"):
            st.session_state.pop("selected_product_id", None)
            st.rerun()

    render_section_header("用户评价")
    reviews = load_product_reviews(product_id=product_id)
    if reviews:
        for review in reviews[:12]:
            render_review(
                review["user_id"],
                review["rating"],
                review["content"],
            )
    else:
        render_empty("这个商品还没有用户评价。")


def render_product_catalog_view():
    taxonomy = load_product_taxonomy()
    search_col, category_col, subcategory_col, price_col = st.columns([1.45, 0.8, 0.8, 0.8])
    with search_col:
        search = st.text_input("搜索商品", key="product_search", placeholder="名称、品牌或描述")
    with category_col:
        category = st.selectbox("商品分类", ["全部", *taxonomy.keys()], key="product_category")
    with subcategory_col:
        subcategories = taxonomy.get(category, []) if category != "全部" else sorted({
            item for values in taxonomy.values() for item in values
        })
        subcategory = st.selectbox("子分类", ["全部", *subcategories], key="product_subcategory")
    with price_col:
        max_price = st.number_input("价格上限", min_value=0, value=20000, step=500, key="product_max_price")

    products = load_products(
        category=category,
        subcategory=subcategory,
        search=search,
        max_price=float(max_price),
    )
    total = len(products)
    st.caption(f"共找到 {total} 件商品")
    if not products:
        render_empty("没有符合当前条件的商品。")
        return

    # 只渲染当前页：几千件商品逐个建按钮会让页面卡十几秒
    signature = f"{category}|{subcategory}|{search}|{max_price}"
    if st.session_state.get("catalog_filter_sig") != signature:
        st.session_state["catalog_filter_sig"] = signature
        st.session_state["catalog_page"] = 1
    pages = max(1, (total + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = min(max(1, int(st.session_state.get("catalog_page", 1))), pages)
    start = (page - 1) * CATALOG_PAGE_SIZE
    visible = products[start:start + CATALOG_PAGE_SIZE]

    for offset in range(0, len(visible), 3):
        cols = st.columns(3)
        for col, product in zip(cols, visible[offset:offset + 3]):
            with col:
                render_card(
                    product["name"],
                    kicker=f"{product.get('brand') or '精选'} · {product.get('subcategory') or product.get('category')}",
                    description=(product.get("description") or "")[:96],
                    meta=(
                        f"¥ {product['price']:.0f}",
                        f"★ {product.get('community_rating') or product.get('rating') or 0:.1f}",
                        f"{product.get('review_count', 0)} 条评价",
                    ),
                    tone="teal",
                )
                if st.button("查看详情", key=f"product_detail_{product['id']}", use_container_width=True):
                    st.session_state["selected_product_id"] = product["id"]
                    st.rerun()

    if pages > 1:
        prev_col, info_col, next_col = st.columns([1, 2, 1])
        if prev_col.button("← 上一页", key="catalog_prev", disabled=page <= 1, use_container_width=True):
            st.session_state["catalog_page"] = page - 1
            st.rerun()
        info_col.caption(f"第 {page} / {pages} 页 · 每页 {CATALOG_PAGE_SIZE} 件")
        if next_col.button("下一页 →", key="catalog_next", disabled=page >= pages, use_container_width=True):
            st.session_state["catalog_page"] = page + 1
            st.rerun()


def render_product_reviews_view():
    taxonomy = load_product_taxonomy()
    search_col, category_col = st.columns([1.55, 0.85])
    with search_col:
        search = st.text_input("搜索评价", key="review_search", placeholder="商品名、评价内容或用户")
    with category_col:
        category = st.selectbox("评价分类", ["全部", *taxonomy.keys()], key="review_category")
    st.caption("最低评分")
    min_rating = render_option_group(
        [1, 2, 3, 4, 5],
        labels=["1 分+", "2 分+", "3 分+", "4 分+", "5 分"],
        key="review_rating",
        default=4,
        columns=5,
    )

    reviews = load_product_reviews(
        category=category,
        min_rating=min_rating,
        search=search,
    )
    st.caption(f"共找到 {len(reviews)} 条评价")
    if not reviews:
        render_empty("没有符合当前条件的评价。")
        return
    for review in reviews[:30]:
        render_review(
            review["user_id"],
            review["rating"],
            review["content"],
            product_name=f"{review['product_name']} · {review.get('brand') or ''}",
        )


def render_travel_detail(resource):
    render_section_header(resource["title"], f"{resource['type']} · 来源 {resource['source']}")
    st.markdown(f"```text\n{resource['content']}\n```")
    if st.button("← 返回生活目录", key="back_to_travel"):
        st.session_state.pop("selected_travel_id", None)
        st.rerun()


def render_travel_catalog_view(resources):
    search_col, type_col = st.columns([1.5, 0.8])
    with search_col:
        search = st.text_input(
            "搜索目的地、活动或关键词",
            key="travel_search",
            placeholder="杭州、露营、周末活动…",
        )
    with type_col:
        types = ["全部", *dict.fromkeys(item["type"] for item in resources)]
        resource_type = st.selectbox("内容类型", types, key="travel_type")

    filtered = filter_travel_resources(
        resources,
        resource_type=resource_type,
        search=search,
    )
    st.caption(f"共找到 {len(filtered)} 条内容")
    if not filtered:
        render_empty("没有符合当前条件的旅行内容。")
        return

    for start in range(0, len(filtered), 3):
        cols = st.columns(3)
        for col, resource in zip(cols, filtered[start:start + 3]):
            with col:
                render_card(
                    resource["title"],
                    kicker=resource["type"],
                    description=resource["description"],
                    meta=resource.get("tags", ())[:2],
                    tone="amber",
                )
                if st.button("查看内容", key=f"travel_detail_{resource['id']}", use_container_width=True):
                    st.session_state["selected_travel_id"] = resource["id"]
                    st.rerun()


def render_catalog_page():
    render_page_header(
        "生活目录",
        "把商品、用户评价、目的地攻略和活动内容放在同一个目录里分类检索。",
        "Life Directory",
    )
    product_summary = load_product_summary()
    travel_resources = load_travel_resources()
    travel_guides = sum(1 for item in travel_resources if item["type"] == "目的地攻略")
    travel_activities = len(travel_resources) - travel_guides

    metric_columns([
        ("商品", product_summary["products"], "分类、详情与价格"),
        ("用户评价", product_summary["reviews"], "评分与真实使用反馈"),
        ("旅行内容", len(travel_resources), f"{travel_guides} 篇攻略"),
        ("活动灵感", travel_activities, "周末、户外与本地活动"),
    ])

    if product_id := st.session_state.get("selected_product_id"):
        render_product_detail(product_id)
        return

    if selected_id := st.session_state.get("selected_travel_id"):
        selected = next(
            (item for item in travel_resources if item["id"] == selected_id),
            None,
        )
        if selected:
            render_travel_detail(selected)
            return

    render_section_header("分类检索", "选择目录视图，按分类、关键词、评分或内容类型浏览")
    view = render_option_group(
        ["商品分类", "商品评价", "目的地攻略", "活动灵感"],
        key="catalog_view",
        default="商品分类",
        columns=4,
    )
    if view == "商品分类":
        render_product_catalog_view()
    elif view == "商品评价":
        render_product_reviews_view()
    elif view == "目的地攻略":
        render_travel_catalog_view([
            item for item in travel_resources
            if item["type"] == "目的地攻略"
        ])
    else:
        render_travel_catalog_view([
            item for item in travel_resources
            if item["type"] != "目的地攻略"
        ])



def render_memory_search():
    render_section_header("记忆检索", "同时搜索向量记忆和 MD 文档记忆")
    query = st.text_input("搜索关键词", key="memory_search_page", placeholder="例如：跑鞋、杭州、预算偏好")
    if not query:
        return
    try:
        from app.memory.long_term import LongTermMemory
        from app.memory.md_memory import MDMemory
        long_term = _long_term_memory()
        md_memory = _md_memory()
        vector_results = long_term.recall(st.session_state["user_id"], query, k=6)
        md_results = md_memory.search_memories(st.session_state["user_id"], query)
    except Exception as exc:
        st.warning(f"检索失败：{exc}")
        return

    left, right = st.columns(2)
    with left:
        st.markdown("#### 向量记忆")
        if vector_results:
            for item in vector_results:
                st.markdown(
                    f"""
                    <div class="review-card">
                      <div class="review-head"><span>相关记忆</span><span>{item.get('score', '')}</span></div>
                      <div class="review-copy">{item.get('content', '')}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            render_empty("没有找到相关向量记忆。")
    with right:
        st.markdown("#### MD 文档记忆")
        if md_results:
            for item in md_results:
                render_review(
                    item.get("type", "记忆"),
                    item.get("category", item.get("event_type", "general")),
                    item.get("content", ""),
                )
        else:
            render_empty("没有找到相关 MD 记忆。")


def render_conversation_summary():
    render_section_header("会话摘要", "选择一条具体对话；同一对话已有摘要时直接复用，不重复生成")
    try:
        from app.memory.conversation_store import ConversationStore
        store = ConversationStore()
        threads = store.list_threads(st.session_state["user_id"])
    except Exception as exc:
        st.warning(f"会话列表加载失败：{exc}")
        return

    if not threads:
        render_empty("还没有可压缩的已归档对话。")
        return

    labels = {
        item["thread_id"]: (
            f"{item.get('scene', 'general')} · {item.get('message_count', 0)} 条 · "
            f"{str(item.get('last_at', ''))[:16]} · {item.get('preview', '')}"
        )
        for item in threads
    }
    thread_id = st.selectbox(
        "选择对话",
        options=[item["thread_id"] for item in threads],
        format_func=lambda value: labels[value],
        key="summary_thread_id",
    )
    messages = store.list_messages(thread_id)
    conversation_hash = store.conversation_hash(messages)
    existing = store.get_summary(thread_id, conversation_hash)
    pending = st.session_state.get("pending_conversation_summary") or {}
    same_pending = (
        pending.get("thread_id") == thread_id
        and pending.get("conversation_hash") == conversation_hash
    )

    if existing:
        st.info("该对话已有摘要，本次直接复用，不再重复生成。")
        st.text_area(
            "已保存摘要",
            value=existing.get("summary", ""),
            key=f"existing_summary_{conversation_hash[:12]}",
            height=180,
            disabled=True,
        )
        return

    if same_pending and pending.get("summary"):
        summary_key = f"conversation_summary_{conversation_hash[:12]}"
        edited_summary = st.text_area(
            "摘要内容",
            value=pending["summary"],
            key=summary_key,
            height=180,
        )
        render_inline_sensitive_action(
            "memory_save_summary",
            {
                "user_id": st.session_state["user_id"],
                "summary": edited_summary,
                "thread_id": thread_id,
                "conversation_hash": conversation_hash,
                "idempotency_key": f"memory-summary-{uuid.uuid4().hex}",
            },
            "保存所选对话的摘要",
            key=f"save_summary_{conversation_hash[:12]}",
            submit_label="保存摘要",
            primary=True,
        )
        if st.button("放弃摘要", key=f"discard_summary_{conversation_hash[:12]}", use_container_width=True):
            st.session_state.pop("pending_conversation_summary", None)
            st.rerun()
        return

    if st.button("生成摘要", key=f"generate_summary_{conversation_hash[:12]}", type="primary"):
        try:
            from app.memory.compressor import MemoryCompressor
            summary = MemoryCompressor().compress(messages)
        except Exception as exc:
            st.error(f"摘要生成失败：{exc}")
            return
        if summary:
            st.session_state["pending_conversation_summary"] = {
                "thread_id": thread_id,
                "conversation_hash": conversation_hash,
                "summary": summary,
            }
            st.rerun()
        else:
            st.info("没有生成摘要内容。")


def render_md_memory_manager():
    render_section_header("记忆管理", "按类型与分类筛选，支持编辑、删除和安全审批")
    try:
        from app.memory.md_memory import MDMemory
        md_memory = _md_memory()
        all_memories = md_memory.get_all_memories(st.session_state["user_id"])
    except Exception as exc:
        st.warning(f"记忆加载失败：{exc}")
        return

    if not all_memories:
        render_empty("还没有已保存的记忆。")
        return

    types = ["全部", *sorted({item.get("type", "general") for item in all_memories})]
    selected_type = st.selectbox("记忆类型", types, key="memory_type_filter")
    selected_category = "全部"

    filtered = [
        item for item in all_memories
        if (selected_type == "全部" or item.get("type") == selected_type)
        and (selected_category == "全部" or str(item.get("category", item.get("event_type", "general"))) == selected_category)
    ]
    st.caption(f"共 {len(filtered)} 条记忆")
    for item in filtered:
        title = item["content"][:48] + ("…" if len(item["content"]) > 48 else "")
        pending_here = bool(
            st.session_state.get(f"pending_inline_delete_{item['id']}")
            or st.session_state.get(f"pending_inline_update_{item['id']}")
        )
        with st.expander(
            f"{item.get('category', item.get('event_type', 'general'))} · {title}",
            expanded=pending_here,
        ):
            meta_col, content_col = st.columns([1, 3])
            with meta_col:
                st.caption(f"类型：{item.get('type', 'general')}")
                st.caption(f"分类：{item.get('category', item.get('event_type', 'general'))}")
                st.caption(f"时间：{item.get('timestamp', '')}")
            with content_col:
                new_content = st.text_area(
                    "记忆内容",
                    value=item["content"],
                    key=f"edit_{item['id']}",
                    height=110,
                )
                delete_col, update_col = st.columns(2)
                with delete_col:
                    render_inline_sensitive_action(
                        "memory_delete",
                        {
                            "user_id": st.session_state["user_id"],
                            "memory_id": item["id"],
                            "idempotency_key": f"memory-delete-{item['id']}-{uuid.uuid4().hex}",
                        },
                        f"删除记忆 {item['id']}",
                        key=f"delete_{item['id']}",
                        submit_label="删除",
                    )
                with update_col:
                    render_inline_sensitive_action(
                        "memory_update",
                        {
                            "user_id": st.session_state["user_id"],
                            "memory_id": item["id"],
                            "content": new_content,
                            "idempotency_key": f"memory-update-{item['id']}-{uuid.uuid4().hex}",
                        },
                        f"更新记忆 {item['id']}",
                        key=f"update_{item['id']}",
                        submit_label="更新",
                    )



def render_last_memory_action_result():
    result = st.session_state.pop("last_memory_action_result", None)
    if not result:
        return
    status = result.get("status")
    payload = result.get("result") or {}
    message = result.get("message", "")
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if status == "error" or errors:
        st.error(message or "记忆操作未完全成功，请检查 Embedding 配置后重试。")
    elif status == "executed":
        st.success("记忆操作已完成")
    elif status == "rejected":
        st.info("已取消记忆操作")
    else:
        st.info("记忆操作未执行")


def render_vector_memory_browser():
    render_section_header("向量记忆", "默认仅显示统计，点击后查看全部向量内容和元数据")
    try:
        from app.memory.long_term import LongTermMemory
        memories = _long_term_memory().list_user_memories(st.session_state["user_id"], limit=200)
    except Exception as exc:
        st.warning(f"向量记忆加载失败：{exc}")
        return

    if not memories:
        render_empty("没有可显示的向量记忆。")
        return

    type_counts: dict[str, int] = {}
    for item in memories:
        metadata = item.get("metadata", {})
        memory_type = str(metadata.get("memory_type", metadata.get("type", "unknown")))
        type_counts[memory_type] = type_counts.get(memory_type, 0) + 1
    summary = "、".join(f"{name} {count} 条" for name, count in sorted(type_counts.items()))

    st.caption(f"共 {len(memories)} 条 · {summary}")
    vector_pending = any(
        key.startswith(("pending_inline_vector_", "pending_inline_update_vector_"))
        and value
        for key, value in st.session_state.items()
    )
    with st.expander(
        f"展开全部向量记忆（{len(memories)} 条）",
        expanded=vector_pending,
    ):
        for item in memories:
            metadata = item.get("metadata", {})
            memory_type = metadata.get("memory_type", metadata.get("type", "memory"))
            title = (item.get("content") or "未命名向量记忆")[:52]
            st.markdown(f"**{memory_type} · {title}**")
            edit_key = f"vector_edit_{item.get('id')}"
            source_key = f"{edit_key}_source"
            current_content = item.get("content", "")
            if st.session_state.get(source_key) != current_content:
                st.session_state[edit_key] = current_content
                st.session_state[source_key] = current_content
            edited_content = st.text_area(
                "向量记忆内容",
                key=edit_key,
                height=120,
            )
            st.caption(
                f"vector_id: {item.get('id')} · memory_id: {metadata.get('memory_id') or '无'} · "
                f"type: {memory_type} · version: {metadata.get('version', '-')}"
            )
            vector_key = str(item.get("id", "")).replace("-", "_")
            memory_id = metadata.get("memory_id")
            update_col, delete_col = st.columns(2)
            with update_col:
                render_inline_sensitive_action(
                    "memory_update_vector",
                    {
                        "user_id": st.session_state["user_id"],
                        "vector_id": item.get("id"),
                        "content": edited_content,
                        "idempotency_key": f"memory-vector-update-{uuid.uuid4().hex}",
                    },
                    "更新向量记忆" + ("及对应 MD" if memory_id else ""),
                    key=f"update_vector_{vector_key}",
                    submit_label="保存修改",
                )
            with delete_col:
                render_inline_sensitive_action(
                    "memory_delete_vector",
                    {
                        "user_id": st.session_state["user_id"],
                        "vector_id": item.get("id"),
                        "idempotency_key": f"memory-vector-delete-{uuid.uuid4().hex}",
                    },
                    "删除向量记忆" + ("及对应 MD" if memory_id else ""),
                    key=f"vector_{vector_key}",
                    submit_label="删除",
                )
            st.divider()


def render_md_document_editor():
    render_section_header("MD 原文编辑", "直接编辑 preferences.md / events.md，保存后同步索引和向量记忆")
    try:
        from app.memory.md_memory import MDMemory
        md_memory = _md_memory()
        user_id = st.session_state["user_id"]
        preferences_content = md_memory.get_raw_document(user_id, "preferences")
        events_content = md_memory.get_raw_document(user_id, "events")
    except Exception as exc:
        st.warning(f"MD 原文加载失败：{exc}")
        return

    for widget_key, current_content in (
        ("raw_preferences_md", preferences_content),
        ("raw_events_md", events_content),
    ):
        source_key = f"{widget_key}_source"
        if st.session_state.get(source_key) != current_content:
            st.session_state[widget_key] = current_content
            st.session_state[source_key] = current_content

    preferences_tab, events_tab = st.tabs(["preferences.md", "events.md"])
    with preferences_tab:
        edited_preferences = st.text_area(
            "偏好记忆 Markdown",
            key="raw_preferences_md",
            height=360,
        )
    with events_tab:
        edited_events = st.text_area(
            "事件记忆 Markdown",
            key="raw_events_md",
            height=360,
        )

    st.caption(
        "不强制手写格式：直接在文档末尾添加普通段落，保存时会自动补 memory_id、分类和时间。"
        "提交后会在同一位置出现“同意/拒绝”；只有显示“记忆操作已完成”才代表向量已经持久化。"
    )
    st.caption("“清理无MD对应向量”只批量删除找不到 MD/index 对应关系的遗留向量，不会删除正常同步的记忆。")
    sync_payload = {
        "user_id": user_id,
        "preferences_content": md_memory.autofmt_document(
            "preferences", edited_preferences, preferences_content
        ),
        "events_content": md_memory.autofmt_document(
            "events", edited_events, events_content
        ),
        "idempotency_key": f"memory-md-sync-{uuid.uuid4().hex}",
    }
    render_inline_sensitive_action(
        "memory_sync_markdown",
        sync_payload,
        "保存 MD 并同步向量记忆",
        key="md_sync",
        submit_label="保存 MD 并同步向量",
        primary=True,
    )
    render_inline_sensitive_action(
        "memory_cleanup_vectors",
        {
            "user_id": user_id,
            "idempotency_key": f"memory-orphan-cleanup-{uuid.uuid4().hex}",
        },
        "批量清理没有 MD/index 对应关系的向量记忆",
        key="orphan_cleanup",
        submit_label="清理无MD对应向量",
    )



def render_memory_page():
    render_page_header(
        "记忆中心",
        "按对话生成摘要，并统一编辑 MD 记忆与向量记忆。",
        "Memory Center",
    )
    render_last_memory_action_result()
    render_memory_entry("general", "memory_center")
    render_memory_extraction_panel("", "memory_center")

    try:
        from app.memory.long_term import LongTermMemory
        from app.memory.md_memory import MDMemory
        long_term = _long_term_memory()
        md_memory = _md_memory()
        vector_count = long_term.get_memory_count(st.session_state["user_id"])
        md_profile = md_memory.get_user_profile(st.session_state["user_id"])
        md_count = md_profile.get("memory_count", 0)
    except Exception:
        vector_count = 0
        md_count = 0
    pending_count = int(bool(st.session_state.get("show_memory_extraction"))) + sum(
        1 for key, value in st.session_state.items()
        if key.startswith("pending_inline_") and value
    )

    metric_columns([
        ("向量记忆", vector_count, "语义召回"),
        ("MD 文档记忆", md_count, "可读、可编辑"),
        ("待确认内容", pending_count, "需要用户决定"),
        ("敏感操作", sum(1 for key, value in st.session_state.items() if key.startswith("pending_inline_") and value), "原位确认"),
    ])

    render_conversation_summary()
    render_vector_memory_browser()
    render_md_document_editor()


def render_model_configuration():
    render_section_header("模型与连接", "配置主模型、摘要模型和 Embedding，并执行连接测试")
    from app.config import get_config
    cfg = get_config()
    main_cfg = cfg.get("main", {})
    small_cfg = cfg.get("small", {})
    embedding_cfg = cfg.get("embedding", {})

    tab_main, tab_small, tab_embedding = st.tabs(["主模型", "摘要模型", "Embedding"])
    with tab_main:
        main_key = st.text_input("API Key", type="password", key="main_key", value=main_cfg.get("api_key", ""))
        main_url = st.text_input("Base URL", key="main_url", value=main_cfg.get("base_url", ""))
        main_model = st.text_input("模型名称", key="main_model_input", value=main_cfg.get("model", ""))
    with tab_small:
        small_key = st.text_input("API Key", type="password", key="small_key", value=small_cfg.get("api_key", ""))
        small_url = st.text_input("Base URL", key="small_url", value=small_cfg.get("base_url", ""))
        small_model = st.text_input("模型名称", key="small_model_input", value=small_cfg.get("model", ""))
    with tab_embedding:
        emb_key = st.text_input("API Key", type="password", key="emb_key", value=embedding_cfg.get("api_key", ""))
        emb_url = st.text_input("Base URL", key="emb_url", value=embedding_cfg.get("base_url", ""))
        emb_model = st.text_input("模型名称", key="emb_model_input", value=embedding_cfg.get("model", ""))

    if main_key and main_model:
        set_main_config(api_key=main_key, base_url=main_url, model=main_model)
    if small_key and small_model:
        set_small_config(api_key=small_key, base_url=small_url, model=small_model)
    if emb_key and emb_model:
        set_embedding_config(api_key=emb_key, base_url=emb_url, model=emb_model)

    try:
        status = get_embedding_status()
        if isinstance(status, dict):
            state = status.get("status") or status.get("state") or ("ok" if status.get("configured") else "missing")
            message = status.get("message") or status.get("msg") or ""
        elif isinstance(status, (tuple, list)) and len(status) >= 2:
            state, message = status[0], status[1]
        else:
            state, message = "missing", str(status)
    except Exception as exc:
        state, message = "missing", f"状态检测失败：{exc}"
    if state == "ok":
        st.success(message or "Embedding 已就绪")
    elif state in {"warning", "fallback"}:
        st.warning(message or "Embedding 使用降级方案")
    else:
        st.error(message or "Embedding 尚未配置")

    action_col, embedding_col = st.columns(2)
    with action_col:
        if st.button("测试主模型", key="test_conn", use_container_width=True):
            if not main_key or not main_model:
                st.error("请填写主模型配置。")
            else:
                with st.spinner("正在连接主模型…"):
                    try:
                        from app.config import create_llm
                        response = create_llm(main_model, max_tokens=10).invoke("say hi")
                        st.success(f"连接成功：{response.content[:30]}")
                    except Exception as exc:
                        st.error(f"连接失败：{exc}")
    with embedding_col:
        if st.button("测试 Embedding", key="test_emb", use_container_width=True):
            if not emb_key or not emb_model:
                st.error("请填写 Embedding 配置。")
            else:
                with st.spinner("正在连接 Embedding…"):
                    try:
                        from app.config import create_embeddings
                        result = create_embeddings().embed_query("测试文本")
                        st.success(f"连接成功，向量维度：{len(result)}")
                    except Exception as exc:
                        st.error(f"连接失败：{exc}")


def render_profile_page():
    render_page_header(
        "个人中心",
        "查看个人资料、订单记录和模型连接设置。",
        "Profile & Settings",
    )
    actor = st.session_state["actor_context"]
    st.markdown(
        '<div class="sl-actor"><span class="sl-actor-kicker">Actor</span>'
        f'<span>当前可信 Actor：<b>{_esc(actor.user_id)}</b></span></div>',
        unsafe_allow_html=True,
    )
    st.caption("Actor 由服务端启动环境确定；页面和模型工具均不能切换到其他用户。")

    user = load_user(st.session_state["user_id"])
    orders = load_orders(st.session_state["user_id"])
    profile_col, orders_col = st.columns([1, 1.25])
    with profile_col:
        render_section_header("个人资料")
        if user:
            render_card(
                user.get("name", st.session_state["user_id"]),
                kicker="账户资料",
                description=user.get("preferences", "暂无偏好说明"),
                meta=(
                    f"年龄 {user.get('age') or '未填写'}",
                    f"月预算 ¥{user.get('budget') or 0:.0f}",
                    user["id"],
                ),
                tone="teal",
            )
        else:
            render_empty("未找到当前用户资料。")
    with orders_col:
        render_section_header("订单记录")
        if orders:
            for order in orders[:8]:
                status_label = {
                    "completed": "已完成",
                    "shipped": "配送中",
                    "pending": "待处理",
                }.get(order.get("status"), order.get("status", "未知"))
                render_card(
                    order["product_name"],
                    kicker=status_label,
                    description=f"{order.get('brand') or ''} · {order.get('category') or ''}",
                    meta=(
                        f"数量 {order.get('quantity', 1)}",
                        f"¥ {order.get('total_price') or 0:.0f}",
                        str(order.get("created_at", ""))[:10],
                    ),
                    tone="amber",
                )
        else:
            render_empty("暂无订单记录。")

    render_model_configuration()


def render_import_page():
    from app.ui.import_center import render_import_page as render_import_center
    render_import_center()


def render_page(page_key: str):
    pages = {
        "assistant": render_assistant_page,
        "catalog": render_catalog_page,
        "memory": render_memory_page,
        "import": render_import_page,
        "profile": render_profile_page,
    }
    pages.get(page_key, render_assistant_page)()


# ========== 应用入口 ==========
page_key = render_app_sidebar()
st.session_state["active_page"] = page_key
render_page(page_key)
