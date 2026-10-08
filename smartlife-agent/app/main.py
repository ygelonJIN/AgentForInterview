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
import asyncio
import json
import sys
import os
import threading
import time
import queue as thread_queue
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import set_main_config, set_small_config, set_embedding_config, get_embedding_status
from app.observability import log_exception, log_warning
from app.async_runner import SharedAsyncRunner
from app.stream_response import StreamResponseBuffer
from app.streaming import deduplicate_process_events, reconcile_tool_events
from app.process_timeline import build_process_timeline, infer_active_step
from app.ui.content import (
    filter_travel_resources,
    get_database_path,
    load_orders,
    load_product,
    load_product_reviews,
    load_product_summary,
    load_product_taxonomy,
    load_products,
    load_travel_resources,
    load_user,
)
from app.ui.theme import (
    NAV_ITEMS,
    inject_theme,
    metric_columns,
    render_card,
    render_empty,
    render_hero,
    render_metric,
    render_page_header,
    render_process_card,
    render_review,
    render_section_header,
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
    st.session_state["user_id"] = "user_001"
if "browser_session_id" not in st.session_state:
    st.session_state["browser_session_id"] = uuid.uuid4().hex
if "orchestrator_v2" not in st.session_state:
    st.session_state["orchestrator_v2"] = None
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
if "rejected_memories" not in st.session_state:
    st.session_state["rejected_memories"] = []
if "pending_compressed_summary" not in st.session_state:
    st.session_state["pending_compressed_summary"] = ""
if "sensitive_action_service" not in st.session_state:
    st.session_state["sensitive_action_service"] = None
if "assistant_msgs" not in st.session_state:
    st.session_state["assistant_msgs"] = []
if "social_msgs" not in st.session_state:
    st.session_state["social_msgs"] = []


def _migrate_legacy_in_memory_conversations():
    """当前会话启动时幂等迁移旧内存消息，避免升级后丢历史。"""
    from app.memory.conversation_store import ConversationStore
    from app.session import build_thread_id

    store = ConversationStore()
    migrations = []
    for scene, key in (
        ("general", "assistant_msgs"),
        ("general", "messages"),
        ("negotiation", "social_msgs"),
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

# ========== Orchestrator 初始化 ==========
def get_orchestrator_v2():
    if st.session_state["orchestrator_v2"] is None:
        try:
            from app.config import has_api_key
            if not has_api_key("main"):
                st.error("❌ 请先在左侧输入 API Key")
                return None
            from app.agents.langgraph_orchestrator import get_langgraph_orchestrator
            st.session_state["orchestrator_v2"] = get_langgraph_orchestrator(model_name=None)
        except Exception as e:
            log_exception("ui.orchestrator_init", e)
            st.error(f"初始化失败: {e}")
            return None
    return st.session_state["orchestrator_v2"]


def _begin_memory_approval(orch, user_id, candidates, scene):
    if not orch or not candidates:
        return None
    result = orch.start_memory_approval(
        user_id,
        st.session_state["browser_session_id"],
        scene,
        candidates,
    )
    st.session_state["memory_approval_id"] = result["approval_id"]
    return result


def _resume_memory_approval(orch, action, selected_indices=None):
    approval_id = st.session_state.get("memory_approval_id")
    if not orch or not approval_id:
        return {"status": "missing_approval", "state": {"saved_ids": []}}
    decision = {"action": action}
    if selected_indices is not None:
        decision["selected_indices"] = selected_indices
    result = orch.resume_memory_approval(approval_id, decision)
    st.session_state.pop("memory_approval_id", None)
    return result


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
    st.session_state["memory_diag"] = ""
    st.session_state["memory_extraction_error"] = False
    st.session_state["show_memory_extraction"] = False
    st.session_state["memory_extraction_scene"] = ""


def _memory_step_event(*, failed=False, no_content=False, background=False):
    if failed:
        name = "记忆提取失败"
        description = "提取未能完成，请查看下方失败诊断"
        status = "error"
    elif background:
        name = "候选记忆确认"
        description = "回答已完成，候选记忆正在后台提取"
        status = "active"
    elif no_content:
        name = "候选记忆确认"
        description = "检查完成，本轮没有需要写入长期记忆的内容"
        status = "completed"
    else:
        name = "候选记忆确认"
        description = "候选项已提取，等待用户确认是否写入长期记忆"
        status = "waiting"
    return {
        "event": "step",
        "data": {
            "step": 4,
            "total": 4,
            "name": name,
            "description": description,
            "status": status,
            "source": "post_process",
        },
    }


def _apply_memory_extraction_payload(orch, user_id, session_id, scene, payload):
    candidates = list(payload.get("candidates", []))
    diagnostic = payload.get("diagnostic", "")
    has_error = bool(payload.get("has_error", payload.get("status") == "error"))

    if not candidates and not has_error:
        _clear_memory_extraction_state()
        return [_memory_step_event(no_content=True)]

    st.session_state["candidate_memories"] = candidates
    st.session_state["memory_diag"] = diagnostic if has_error else ""
    st.session_state["memory_extraction_error"] = has_error
    st.session_state["show_memory_extraction"] = True
    st.session_state["memory_extraction_scene"] = scene

    if candidates:
        _begin_memory_approval(orch, user_id, candidates, scene)
    return [_memory_step_event(failed=has_error)]


def _extract_memories_after_stream(
    orch,
    user_id,
    session_id,
    scene,
    stream_events=None,
):
    """消费图节点的记忆提取结果；兼容旧的图外提取接口。"""
    if not orch:
        _clear_memory_extraction_state()
        return []

    payload = None
    for event in stream_events or []:
        if event.get("event") == "memory_extraction":
            payload = event.get("data", {}) or {}
            break

    if payload is not None:
        return _apply_memory_extraction_payload(orch, user_id, session_id, scene, payload)

    if hasattr(orch, "pop_memory_extraction_result"):
        payload = orch.pop_memory_extraction_result(user_id, session_id, scene)
        if payload is not None:
            return _apply_memory_extraction_payload(orch, user_id, session_id, scene, payload)
        if hasattr(orch, "is_memory_extraction_pending") and orch.is_memory_extraction_pending(
            user_id, session_id, scene
        ):
            st.session_state["pending_background_memory"] = {
                "user_id": user_id,
                "session_id": session_id,
                "scene": scene,
            }
            return [_memory_step_event(background=True)]
        _clear_memory_extraction_state()
        return [_memory_step_event(no_content=True)]

    if hasattr(orch, "extract_candidate_memories_result"):
        result = orch.extract_candidate_memories_result(
            user_id,
            session_id,
            scene=scene,
            excluded_contents=st.session_state.get("rejected_memories", []),
        )
        return _apply_memory_extraction_payload(
            orch,
            user_id,
            session_id,
            scene,
            {
                "candidates": result.candidates,
                "diagnostic": result.diagnostic,
                "has_error": result.has_error,
                "status": "error" if result.has_error else "ok",
            },
        )

    _clear_memory_extraction_state()
    return [_memory_step_event(no_content=True)]


def _refresh_background_memory_extraction():
    pending = st.session_state.get("pending_background_memory")
    if not pending:
        return
    orch = get_orchestrator_v2()
    if not orch or not hasattr(orch, "pop_memory_extraction_result"):
        return
    payload = orch.pop_memory_extraction_result(
        pending["user_id"],
        pending["session_id"],
        pending["scene"],
    )
    if payload is None:
        return
    _apply_memory_extraction_payload(
        orch,
        pending["user_id"],
        pending["session_id"],
        pending["scene"],
        payload,
    )
    st.session_state.pop("pending_background_memory", None)


def render_memory_extraction_panel(scene, key_suffix):
    """只在有候选记忆或提取失败时渲染结果；无价值诊断默认隐藏。"""
    if not st.session_state.get("show_memory_extraction"):
        return
    if scene and st.session_state.get("memory_extraction_scene") != scene:
        return

    has_error = bool(st.session_state.get("memory_extraction_error"))
    candidates = st.session_state.get("candidate_memories", [])
    if not candidates and not has_error:
        return

    if has_error:
        st.subheader("⚠️ 记忆提取失败")
        st.error("本轮记忆提取未能完成。")
        diagnostic = st.session_state.get("memory_diag", "")
        if diagnostic:
            with st.expander("🔍 失败诊断"):
                st.code(diagnostic)
        if st.button("👌 知道了", key=f"dismiss_memory_error_{key_suffix}"):
            _clear_memory_extraction_state()
            st.rerun()
        return

    st.subheader("📝 记忆提取结果")
    _orch = get_orchestrator_v2()
    selected_indices = []
    for i, candidate in enumerate(candidates):
        emoji = {"shopping": "🛍️", "travel": "✈️", "general": "📝"}.get(
            candidate.get("category", "general"), "📝"
        )
        confidence = {"high": "高", "medium": "中", "low": "低"}.get(
            candidate.get("confidence", "medium"), "中"
        )
        content_col, choice_col = st.columns([2.45, 1])
        with content_col:
            st.write(f"{i + 1}. {emoji} {candidate['content']} (置信度: {confidence})")
        with choice_col:
            should_save = render_option_group(
                [False, True],
                labels=["不保存", "保存这条"],
                key=f"memory_choice_{key_suffix}_{i}",
                default=False,
                columns=2,
            )
            if should_save:
                selected_indices.append(i)

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("✅ 确认写入选中", key=f"save_selected_memories_{key_suffix}"):
            if selected_indices and _orch:
                result = _resume_memory_approval(_orch, "save_selected", selected_indices)
                st.success(f"已保存 {len(result['state']['saved_ids'])} 条记忆")
                _clear_memory_extraction_state()
                st.rerun()
            else:
                st.warning("请先选择要保存的记忆")
    with col2:
        if st.button("❌ 拒绝写入", key=f"skip_memories_{key_suffix}"):
            if _orch:
                _resume_memory_approval(_orch, "reject")
            st.session_state["rejected_memories"].extend(
                candidate["content"] for candidate in candidates
            )
            _clear_memory_extraction_state()
            st.rerun()
    with col3:
        if st.button("💾 全部写入", key=f"save_all_memories_{key_suffix}"):
            if _orch:
                result = _resume_memory_approval(_orch, "save_all")
                st.success(f"已保存 {len(result['state']['saved_ids'])} 条记忆")
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


def _execution_logs_html(events):
    _, logs = _split_execution_logs(events)
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
        '<details class="execution-log-panel" open>'
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


def _process_events_html(events, include_transient=False, active_step=None):
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
    return (
        "".join(cards)
        + _timing_summary_html(events)
        + _execution_logs_html(persistent)
    )


def render_process_events(events, include_transient=False):
    """把一组过程事件按顺序渲染成样式卡片（步骤在前、与正文区分）。"""
    cards = _process_events_html(
        events,
        include_transient=include_transient,
        active_step=4,
    )
    if cards:
        st.markdown(cards, unsafe_allow_html=True)


def render_assistant_message(msg):
    """渲染一条助手消息：过程步骤固定在前，模型回复正文固定在后。"""
    if isinstance(msg.get("process_events"), list) or "response" in msg:
        render_process_events(msg.get("process_events", []), include_transient=False)
        resp = msg.get("response", "")
        if resp:
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


def render_app_sidebar():
    with st.sidebar:
        st.markdown(
            """
            <div class="sidebar-brand">
              <div class="sidebar-kicker">SMARTLIFE</div>
              <div class="sidebar-title">智能生活工作台</div>
              <div class="sidebar-copy">一个入口，自主调度购物、旅行与通用智能体。</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        current = st.session_state.get("active_page", "assistant")
        for _icon, label, page_key in NAV_ITEMS:
            if st.button(
                label,
                key=f"nav_{page_key}",
                use_container_width=True,
                type="primary" if current == page_key else "secondary",
            ):
                st.session_state["active_page"] = page_key
                st.rerun()
        st.markdown("---")
        pending_count = int(bool(st.session_state.get("show_memory_extraction"))) + sum(
            1 for key, value in st.session_state.items()
            if key.startswith("pending_inline_") and value
        )
        if pending_count:
            st.info(f"有 {pending_count} 项内容等待处理，可在记忆中心完成。")
        else:
            st.caption("Agent 自动路由已就绪")
        st.caption(f"当前用户 · {st.session_state['user_id']}")
        return current


def render_quick_prompt(label: str, prompt: str, key: str):
    if st.button(label, key=key, use_container_width=True):
        st.session_state["pending_assistant"] = prompt
        st.rerun()


def render_assistant_page():
    render_hero(
        "Unified Agent Workspace",
        "说你想做什么，剩下的交给智能体",
        "系统会理解任务、自主选择合适的 Agent，并把分类、检索、工具调用和生成过程透明地展示出来。",
    )
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        render_quick_prompt("挑选一双雨天跑鞋", "帮我推荐一双适合雨天跑步、预算 800 元以内的跑鞋", "quick_shoes")
    with c2:
        render_quick_prompt("规划周末旅行", "帮我规划一个杭州周末两日游，预算 1500 元", "quick_trip")
    with c3:
        render_quick_prompt("对比热门手机", "对比一下小米 14、iPhone 15 Pro 和华为 Mate 60 Pro", "quick_compare")
    with c4:
        render_quick_prompt("制定出行清单", "准备去露营三天，帮我列一份装备清单", "quick_list")

    render_section_header("智能对话", "购物、旅行、通用问题都可以从这里开始")
    mode = render_option_group(
        ["智能对话", "多人协商"],
        key="assistant_mode",
        default="智能对话",
        columns=2,
    )

    if mode == "多人协商":
        render_negotiation_workspace()
        return

    if "assistant_msgs" not in st.session_state:
        st.session_state["assistant_msgs"] = []

    _refresh_background_memory_extraction()

    for msg in st.session_state["assistant_msgs"]:
        with st.chat_message(msg["role"]):
            render_assistant_message(msg)

    if st.session_state.get("show_memory_extraction"):
        st.info("本轮已生成待确认的记忆内容，可前往记忆中心查看和决定是否保存。")

    if pending := st.session_state.get("pending_assistant"):
        st.session_state.pop("pending_assistant", None)
        st.session_state["assistant_msgs"].append({"role": "user", "content": pending})
        with st.chat_message("user"):
            st.markdown(pending)
        with st.chat_message("assistant"):
            orch = get_orchestrator_v2()
            uid = st.session_state["user_id"]
            browser_session_id = st.session_state["browser_session_id"]
            if orch:
                async def _gen():
                    async for line in orch.process_streaming(
                        pending,
                        uid,
                        browser_session_id,
                        scene="general",
                    ):
                        yield line

                def _postprocess(_response, stream_events):
                    return _extract_memories_after_stream(
                        orch,
                        uid,
                        browser_session_id,
                        "general",
                        stream_events,
                    )

                response, events = run_streaming_realtime(_gen, postprocess=_postprocess)
            else:
                response = "请先在个人中心配置主模型后开始对话。"
                events = []
                st.warning(response)
        st.session_state["assistant_msgs"].append({
            "role": "assistant",
            "response": response,
            "process_events": events,
        })
        st.rerun()

    if user_input := st.chat_input(
        "描述购物、旅行或任何问题，系统会自动选择 Agent…",
        key="assistant_input",
    ):
        st.session_state["pending_assistant"] = user_input
        st.rerun()


def render_negotiation_workspace():
    st.caption("用于多人旅行或购物决策。填写参与者与约束后，由协商 Agent 寻找共同方案。")
    left, right = st.columns([1, 1])
    with left:
        participant_ids_text = st.text_input(
            "参与者 ID",
            value=st.session_state["user_id"],
            key="negotiation_participant_ids",
        )
    with right:
        participant_budgets_text = st.text_input(
            "每人预算",
            value="",
            key="negotiation_budgets",
            placeholder="例如：2000,3000",
        )
    participant_styles_text = st.text_area(
        "偏好风格",
        value=st.session_state.get("negotiation_styles", ""),
        key="negotiation_styles_area",
        placeholder="多人用 | 分隔，每人偏好用逗号分隔",
        height=88,
    )

    participant_ids = [item.strip() for item in participant_ids_text.split(",") if item.strip()]
    budget_values = [item.strip() for item in participant_budgets_text.split(",")]
    style_groups = [item.strip() for item in participant_styles_text.split("|")]
    participants = []
    for index, participant_id in enumerate(participant_ids):
        budget = 0.0
        if index < len(budget_values):
            try:
                budget = float(budget_values[index])
            except ValueError:
                budget = 0.0
        styles = [
            item.strip()
            for item in (style_groups[index] if index < len(style_groups) else "").split(",")
            if item.strip()
        ]
        participants.append({
            "user_id": participant_id,
            "budget_constraint": budget,
            "travel_preferences": {"preferred_styles": styles},
        })

    if "social_msgs" not in st.session_state:
        st.session_state["social_msgs"] = []
    for msg in st.session_state["social_msgs"][-8:]:
        with st.chat_message(msg["role"]):
            render_assistant_message(msg)

    if prompt := st.chat_input("描述需要协商的场景…", key="social_input"):
        st.session_state["social_msgs"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            orch = get_orchestrator_v2()
            if orch:
                async def _gen():
                    async for line in orch.process_negotiation_streaming(
                        participants,
                        scenario="travel",
                    ):
                        yield line
                response, events = run_streaming_realtime(_gen)
            else:
                response = "请先在个人中心配置主模型。"
                events = []
                st.warning(response)
        st.session_state["social_msgs"].append({
            "role": "assistant",
            "content": response,
            "process_events": events,
        })
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
    st.caption(f"共找到 {len(products)} 件商品")
    if not products:
        render_empty("没有符合当前条件的商品。")
        return

    for start in range(0, len(products), 3):
        cols = st.columns(3)
        for col, product in zip(cols, products[start:start + 3]):
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
        long_term = LongTermMemory()
        md_memory = MDMemory()
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
        md_memory = MDMemory()
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
        memories = LongTermMemory().list_user_memories(st.session_state["user_id"], limit=200)
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
        md_memory = MDMemory()
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
    render_memory_extraction_panel("", "memory_center")

    try:
        from app.memory.long_term import LongTermMemory
        from app.memory.md_memory import MDMemory
        long_term = LongTermMemory()
        md_memory = MDMemory()
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
    user_id_col, save_col = st.columns([2, 1])
    with user_id_col:
        user_id = st.text_input("用户 ID", value=st.session_state["user_id"], key="profile_user_id")
    with save_col:
        st.markdown('<div class="form-action-spacer"></div>', unsafe_allow_html=True)
        if st.button("切换用户", key="switch_user", use_container_width=True):
            st.session_state["user_id"] = user_id.strip() or "user_001"
            st.session_state["orchestrator_v2"] = None
            st.rerun()
    st.session_state["user_id"] = user_id.strip() or "user_001"

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
