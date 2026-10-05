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
import queue as thread_queue
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import set_main_config, set_small_config, set_embedding_config, get_embedding_status
from app.observability import log_exception, log_warning
from app.async_runner import SharedAsyncRunner
from app.stream_response import StreamResponseBuffer
from app.streaming import deduplicate_process_events
from app.process_timeline import build_process_timeline, infer_active_step

st.set_page_config(
    page_title="SmartLife Agent v2",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ========== 样式 ==========
st.markdown("""
<style>
    .main-header { font-size: 2.5rem; font-weight: bold; color: #1f77b4; text-align: center; margin-bottom: 1rem; }
    .metric-card { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 1rem; border-radius: 0.5rem; color: white; text-align: center; }
    .event-thinking { color: #6c757d; font-style: italic; padding: 2px 0; }
    .event-classify { background: #e8f5e9; padding: 8px 12px; border-radius: 6px; margin: 4px 0; border-left: 3px solid #4caf50; }
    .event-tool { background: #fff3e0; padding: 8px 12px; border-radius: 6px; margin: 4px 0; border-left: 3px solid #ff9800; }
    .event-step { background: #e3f2fd; padding: 8px 12px; border-radius: 6px; margin: 4px 0; border-left: 3px solid #2196f3; }
    .event-step.pending { opacity: .62; border-left-color: #90a4ae; }
    .event-step.active { border-left-color: #1976d2; box-shadow: 0 0 0 1px #bbdefb; }
    .event-step.completed { border-left-color: #43a047; }
    .event-step.waiting { border-left-color: #fb8c00; }
    .event-step.error { border-left-color: #e53935; }
    .event-tool-result { background: #e8f5e9; padding: 8px 12px; border-radius: 6px; margin: 4px 0; border-left: 3px solid #43a047; }
    .event-error { background: #ffebee; padding: 8px 12px; border-radius: 6px; margin: 4px 0; border-left: 3px solid #f44336; }
    .event-done { background: #f3e5f5; padding: 8px 12px; border-radius: 6px; margin: 4px 0; border-left: 3px solid #9c27b0; }
</style>
""", unsafe_allow_html=True)

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
if "pending_sensitive_action" not in st.session_state:
    st.session_state["pending_sensitive_action"] = None

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


def _queue_sensitive_action(action_type, payload, description):
    service = get_sensitive_action_service()
    result = service.start(
        action_type,
        st.session_state["user_id"],
        payload,
    )
    st.session_state["pending_sensitive_action"] = {
        "approval_id": result["approval_id"],
        "action_type": action_type,
        "payload": payload,
        "description": description,
    }
    st.rerun()


def render_sensitive_action_approval():
    pending = st.session_state.get("pending_sensitive_action")
    if not pending:
        return

    st.warning(f"⚠️ 需要确认敏感操作：{pending['description']}")
    st.json(pending.get("payload", {}))
    approve_col, reject_col = st.columns(2)
    with approve_col:
        if st.button("✅ 批准执行", key="approve_sensitive_action"):
            service = get_sensitive_action_service()
            result = service.resume(pending["approval_id"], {
                "action": "approve",
                "idempotency_key": pending.get("payload", {}).get("idempotency_key"),
            })
            st.success(f"操作已完成：{result.get('status')}")
            if pending.get("action_type") == "memory_save_summary":
                st.session_state.pop("pending_compressed_summary", None)
                st.session_state.pop("pending_compressed_scene", None)
            st.session_state["pending_sensitive_action"] = None
            st.rerun()
    with reject_col:
        if st.button("❌ 拒绝执行", key="reject_sensitive_action"):
            service = get_sensitive_action_service()
            service.resume(pending["approval_id"], {"action": "reject"})
            st.info("已拒绝敏感操作")
            st.session_state["pending_sensitive_action"] = None
            st.rerun()


def _clear_memory_extraction_state():
    st.session_state["candidate_memories"] = []
    st.session_state["memory_diag"] = ""
    st.session_state["memory_extraction_error"] = False
    st.session_state["show_memory_extraction"] = False
    st.session_state["memory_extraction_scene"] = ""


def _memory_step_event(*, failed=False, no_content=False):
    if failed:
        name = "记忆提取失败"
        description = "提取未能完成，请查看下方失败诊断"
        status = "error"
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
        candidates = list(payload.get("candidates", []))
        diagnostic = payload.get("diagnostic", "")
        has_error = bool(payload.get("has_error", payload.get("status") == "error"))
    else:
        result = orch.extract_candidate_memories_result(
            user_id,
            session_id,
            scene=scene,
            excluded_contents=st.session_state.get("rejected_memories", []),
        )
        candidates = result.candidates
        diagnostic = result.diagnostic
        has_error = result.has_error

    if not candidates and not has_error:
        # Step 4 仍需展示实际完成状态，但不显示无价值诊断。
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


def render_memory_extraction_panel(scene, key_suffix):
    """只在有候选记忆或提取失败时渲染结果；无价值诊断默认隐藏。"""
    if not st.session_state.get("show_memory_extraction"):
        return
    if st.session_state.get("memory_extraction_scene") != scene:
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
        col1, col2 = st.columns([4, 1])
        with col1:
            st.write(f"{i + 1}. {emoji} {candidate['content']} (置信度: {confidence})")
        with col2:
            if st.checkbox("保存", key=f"memory_{key_suffix}_{i}", value=False):
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
                st.warning("请先勾选要保存的记忆")
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


def _fmt_event_html(evt):
    """把单个过程事件渲染成带样式的 HTML 卡片（与模型正文明显区分）。"""
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
        return ('<div class="event-classify">🎯 <b>分类结果</b><br>'
                f'路由: {_esc(route)} | 意图: {_esc(intent)} | 子意图: {_esc(sub)}<br>'
                f'检索策略: {_esc(retr)} | 置信度: {_esc(conf)}<br>'
                f'原因: {_esc(reason)} | 方法: {_esc(method)}</div>')
    if et == "step":
        step_num = data.get("step", 0)
        total = data.get("total", 0)
        name = data.get("name", "")
        desc = data.get("description", "")
        source = data.get("source", "")
        status = data.get("status", "active")
        status_text = {
            "pending": "🕒 等待中",
            "active": "⏳ 进行中",
            "completed": "✅ 已完成",
            "waiting": "✋ 等待确认",
            "error": "❌ 失败",
        }.get(status, "⏳ 进行中")
        source_label = f"<br><small>来源：{_esc(source)}</small>" if source else ""
        return (f'<div class="event-step {_esc(status)}">📋 <b>Step {step_num}/{total}: {_esc(name)}</b>'
                f' &nbsp; {_esc(status_text)}<br>'
                f'{_esc(desc)}{source_label}</div>')
    if et == "tool_call":
        tn = data.get("tool", "")
        if tn in {"行程执行", "计划自检", "计划审核"}:
            return ""
        ti = (data.get("input", "") or "").replace("\n", " ").strip()
        if tn == "nl2sql":
            short_sql = ti if len(ti) <= 160 else ti[:160] + "…"
            return ('<div class="event-tool">🔧 <b>SQL 数据检索</b><br>'
                    '生成并执行 SQL 查询商品数据<br>'
                    f'<code>{_esc(short_sql)}</code></div>')
        if tn == "rag":
            return f'<div class="event-tool">📚 <b>RAG 评价检索</b><br>{_esc(ti)}</div>'
        return f'<div class="event-tool">⚙️ <b>{_esc(tn)}</b><br>{_esc(ti)}</div>'
    if et == "tool_result":
        tn = data.get("tool", "")
        if tn in {"行程执行", "计划自检", "计划审核"}:
            return ""
        output = (data.get("output", "") or "").replace("\n", " ").strip()
        return f'<div class="event-tool-result">✅ <b>{_esc(tn)}</b><br>{_esc(output)}</div>'
    if et == "error":
        return f'<div class="event-error">❌ <b>错误</b>: {_esc(data.get("message", ""))}</div>'
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
    source_events = build_process_timeline(
        deduplicate_process_events(
        events if include_transient else _persistent_events(events)
        ),
        active_step=active_step or infer_active_step(events),
    )
    cards = []
    for evt in source_events:
        h = _fmt_event_html(evt)
        if h:
            cards.append(h)
    return "".join(cards)


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
    if evt.get("event") == "response_reset":
        return True
    data = evt.get("data", {}) or {}
    text = " ".join(str(data.get(key, "")) for key in ("message", "input", "output"))
    return any(marker in text for marker in ("优化计划", "修订", "改进版", "审核意见"))


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
                response_buffer.reset()
                response_container.empty()

            response_buffer.handle_event(evt)

            if event_type == "token":
                response_container.markdown(response_buffer.text)
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
                response_container.empty()
                continue

            if event_type == "done":
                if response_buffer.text:
                    response_container.markdown(response_buffer.text)
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


# ========== 侧边栏配置 ==========
with st.sidebar:
    st.title("🤖 SmartLife Agent v2")
    st.caption("全链路 Streaming 智能体")
    st.markdown("---")

    st.subheader("👤 用户设置")
    user_id = st.text_input("用户ID", value=st.session_state["user_id"])
    st.session_state["user_id"] = user_id

    st.markdown("---")
    st.subheader("🔧 主模型配置")
    from app.config import get_config as _get_cfg
    _cfg = _get_cfg()
    _mc = _cfg.get("main", {})
    _sc = _cfg.get("small", {})
    main_key = st.text_input("主模型 API Key", type="password", key="main_key", value=_mc.get("api_key",""))
    main_url = st.text_input("主模型 Base URL", value=_mc.get("base_url",""), key="main_url")
    main_model = st.text_input("主模型名称", value=_mc.get("model",""), key="main_model_input")

    st.markdown("---")
    st.subheader("🧠 小模型配置（可选）")
    small_key = st.text_input("小模型 API Key", type="password", key="small_key", value=_sc.get("api_key",""))
    small_url = st.text_input("小模型 Base URL", value=_sc.get("base_url",""), key="small_url")
    small_model = st.text_input("小模型名称", value=_sc.get("model",""), key="small_model_input")

    st.markdown("---")
    st.subheader("📊 Embedding模型配置")
    st.caption("用于 RAG 向量检索，将文本转为向量表示")
    
    _ec = _cfg.get("embedding", {})
    emb_key = st.text_input("Embedding API Key", type="password", key="emb_key", value=_ec.get("api_key",""))
    emb_url = st.text_input("Embedding Base URL", value=_ec.get("base_url",""), key="emb_url")
    emb_model = st.text_input(
        "Embedding模型名称",
        value=_ec.get("model",""),
        key="emb_model_input",
        help="常用模型：\n- OpenAI: text-embedding-3-small, text-embedding-3-large\n- 智谱: embedding-3\n- 通义: text-embedding-v3\n- 百度: bge-large-zh\n- 本地: text2vec-large-chinese"
    )

    # 配置状态提示（兼容元组/字典/异常等不同返回结构，避免崩溃）
    try:
        _emb_st = get_embedding_status()
        if isinstance(_emb_st, dict):
            emb_status = _emb_st.get("status") or _emb_st.get("state") or (
                "ok" if _emb_st.get("configured") else "missing"
            )
            emb_msg = _emb_st.get("message") or _emb_st.get("msg") or ""
        elif isinstance(_emb_st, (tuple, list)) and len(_emb_st) >= 2:
            emb_status, emb_msg = _emb_st[0], _emb_st[1]
        else:
            emb_status, emb_msg = "missing", str(_emb_st)
    except Exception as _emb_err:
        log_warning("ui.embedding_status", str(_emb_err))
        emb_status, emb_msg = "missing", f"Embedding 状态检测异常: {_emb_err}"
    if emb_status == "ok":
        st.success(f"✅ {emb_msg}")
    elif emb_status == "warning":
        st.warning(emb_msg)
    elif emb_status == "fallback":
        st.info(f"ℹ️ {emb_msg}")
    elif emb_status == "missing":
        st.error(f"❌ {emb_msg}")

    if main_key and main_model:
        set_main_config(api_key=main_key, base_url=main_url, model=main_model)
    if small_key and small_model:
        set_small_config(api_key=small_key, base_url=small_url, model=small_model)
    if emb_key and emb_model:
        set_embedding_config(api_key=emb_key, base_url=emb_url, model=emb_model)

    if st.button("🔌 测试主模型连接", key="test_conn"):
        if not main_key or not main_model:
            st.error("请填写主模型配置")
        else:
            with st.spinner("测试主模型中..."):
                try:
                    from app.config import create_llm
                    llm = create_llm(main_model, max_tokens=10)
                    resp = llm.invoke("say hi")
                    st.success(f"✅ 主模型连接成功！{resp.content[:30]}")
                except Exception as e:
                    st.error(f"❌ 主模型失败: {e}")

    if st.button("📊 测试Embedding连接", key="test_emb"):
        if not emb_key or not emb_model:
            st.error("请填写Embedding配置")
        else:
            with st.spinner("测试Embedding中..."):
                try:
                    from app.config import create_embeddings
                    embeddings = create_embeddings()
                    result = embeddings.embed_query("hello")
                    st.success(f"✅ Embedding连接成功！向量维度: {len(result)}")
                except Exception as e:
                    st.error(f"❌ Embedding失败: {e}")

    st.markdown("---")
    
    # 记忆管理
    st.subheader("🧠 记忆管理")
    
    if st.button("🗑️ 清空所有记忆", key="clear_all_memories"):
        _queue_sensitive_action(
            "memory_clear",
            {
                "user_id": st.session_state["user_id"],
                "idempotency_key": f"memory-clear-{uuid.uuid4().hex}",
            },
            "清空该用户的全部长期记忆",
        )

    st.markdown("---")
    st.subheader("📖 架构说明")
    st.markdown("""
    **Streaming 全链路：**
    1. ⚡ 小模型分类（路由+意图+检索）
    2. 🔧 智能检索（SQL/RAG/混合）
    3. 🧠 大模型流式生成

    **记忆系统：**
    - 短期记忆：内存会话
    - 长期记忆：向量库 + MD 文档
    - 手动保存：用户决定是否保留
    """)


# ========== 主界面 ==========
st.markdown('<div class="main-header">🤖 SmartLife Agent v2</div>', unsafe_allow_html=True)
render_sensitive_action_approval()

tab1, tab2, tab3, tab4 = st.tabs(["🛒 购物场景", "✈️ 旅游场景", "🤝 社交协商", "👤 个人中心"])

# ===== Tab1: 购物场景 (Streaming) =====
with tab1:
    st.subheader("🛒 智能购物助手")

    if "shopping_msgs" not in st.session_state:
        st.session_state["shopping_msgs"] = []

    # 1. 渲染持久化对话历史
    for msg in st.session_state["shopping_msgs"]:
        with st.chat_message(msg["role"]):
            render_assistant_message(msg)

    # 2. 记忆提取面板（仅显示真实候选或失败；无价值诊断不展示）
    render_memory_extraction_panel("shopping", "shopping")

    # 3. 处理待处理的新消息（渲染响应 + 提取记忆）
    if st.session_state.get("pending_shopping"):
        prompt = st.session_state.pop("pending_shopping")
        st.session_state["shopping_msgs"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            orch = get_orchestrator_v2()
            _uid = st.session_state["user_id"]
            browser_session_id = st.session_state["browser_session_id"]
            if orch:
                async def _gen():
                    async for line in orch.process_streaming(
                        prompt, _uid, browser_session_id, scene="shopping"
                    ):
                        yield line

                def _postprocess(_response, stream_events):
                    return _extract_memories_after_stream(
                        orch,
                        _uid,
                        browser_session_id,
                        "shopping",
                        stream_events,
                    )

                response, _events = run_streaming_realtime(
                    _gen,
                    postprocess=_postprocess,
                )
            else:
                response = "请先在左侧配置 API Key"
                _events = []
                st.warning(response)
        st.session_state["shopping_msgs"].append({
            "role": "assistant", "process_events": _events, "response": response
        })
        st.rerun()

    # 4. 输入框永远在最后
    if user_input := st.chat_input("描述你的购物需求...", key="shopping_input"):
        st.session_state["pending_shopping"] = user_input
        st.rerun()

# ===== Tab2: 旅游场景 =====
with tab2:
    st.subheader("✈️ 智能旅游助手")

    if "travel_msgs" not in st.session_state:
        st.session_state["travel_msgs"] = []

    # 1. 渲染持久化对话历史
    for msg in st.session_state["travel_msgs"]:
        with st.chat_message(msg["role"]):
            render_assistant_message(msg)

    # 2. 记忆提取面板（仅显示真实候选或失败；无价值诊断不展示）
    render_memory_extraction_panel("travel", "travel")

    # 3. 处理待处理的新消息（渲染响应 + 提取记忆）
    if st.session_state.get("pending_travel"):
        prompt = st.session_state.pop("pending_travel")
        st.session_state["travel_msgs"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            orch = get_orchestrator_v2()
            _uid = st.session_state["user_id"]
            browser_session_id = st.session_state["browser_session_id"]
            if orch:
                async def _gen():
                    async for line in orch.process_streaming(
                        prompt, _uid, browser_session_id, scene="travel"
                    ):
                        yield line

                def _postprocess(_response, stream_events):
                    return _extract_memories_after_stream(
                        orch,
                        _uid,
                        browser_session_id,
                        "travel",
                        stream_events,
                    )

                response, _events = run_streaming_realtime(
                    _gen,
                    postprocess=_postprocess,
                )
            else:
                response = "请先在左侧配置 API Key"
                _events = []
                st.warning(response)
        st.session_state["travel_msgs"].append({
            "role": "assistant", "process_events": _events, "response": response
        })
        st.rerun()

    # 4. 输入框永远在最后
    if user_input := st.chat_input("描述你的旅行计划...", key="travel_input"):
        st.session_state["pending_travel"] = user_input
        st.rerun()

# ===== Tab3: 社交协商 =====
with tab3:
    st.subheader("🤝 社交协商模拟")

    if "social_msgs" not in st.session_state:
        st.session_state["social_msgs"] = []

    participant_ids_text = st.text_input(
        "参与者 ID（逗号分隔）",
        value=st.session_state["user_id"],
        key="negotiation_participant_ids",
    )
    participant_budgets_text = st.text_input(
        "每人预算（逗号分隔，可选）",
        value="",
        key="negotiation_budgets",
    )
    participant_styles_text = st.text_input(
        "每人偏好风格（逗号分隔；多人用 | 分隔，可选）",
        value="",
        key="negotiation_styles",
    )

    participant_ids = [item.strip() for item in participant_ids_text.split(",") if item.strip()]
    budget_values = [item.strip() for item in participant_budgets_text.split(",")]
    style_groups = [item.strip() for item in participant_styles_text.split("|")]
    negotiation_participants = []
    for index, participant_id in enumerate(participant_ids):
        budget = 0.0
        if index < len(budget_values):
            try:
                budget = float(budget_values[index])
            except ValueError:
                budget = 0.0
        styles = [
            style.strip()
            for style in (style_groups[index] if index < len(style_groups) else "").split(",")
            if style.strip()
        ]
        negotiation_participants.append({
            "user_id": participant_id,
            "budget_constraint": budget,
            "travel_preferences": {"preferred_styles": styles},
        })

    for msg in st.session_state["social_msgs"][-6:]:
        with st.chat_message(msg["role"]):
            render_assistant_message(msg)

    if prompt := st.chat_input("描述协商场景...", key="social_input"):
        st.session_state["social_msgs"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            orch = get_orchestrator_v2()
            _uid = st.session_state["user_id"]
            if orch:
                async def _gen():
                    async for line in orch.process_negotiation_streaming(
                        negotiation_participants,
                        scenario="travel",
                    ):
                        yield line
                response, _ = run_streaming_realtime(_gen)
            else:
                response = "请先在左侧配置 API Key"
                st.warning(response)

        st.session_state["social_msgs"].append({"role": "assistant", "content": response})

# ===== Tab4: 个人中心 =====
with tab4:
    st.subheader("个人中心")

    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "products.db")
    c1, c2, c3 = st.columns(3)

    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE id=?", (st.session_state["user_id"],))
        user = cur.fetchone()

        if user:
            with c1:
                st.markdown('<div class="metric-card"><h3>👤 用户信息</h3></div>', unsafe_allow_html=True)
                st.write(f"**姓名:** {user['name']}")
                st.write(f"**年龄:** {user['age']}")
                st.write(f"**偏好:** {user['preferences']}")
                st.write(f"**月预算:** ¥{user['budget']}")
        else:
            with c1:
                st.markdown('<div class="metric-card"><h3>👤 用户信息</h3></div>', unsafe_allow_html=True)
                st.info(f"未找到用户 {st.session_state['user_id']}")

        cur.execute("""
            SELECT o.*, p.name as product_name, p.price
            FROM orders o JOIN products p ON o.product_id = p.id
            WHERE o.user_id = ? ORDER BY o.created_at DESC
        """, (st.session_state["user_id"],))
        orders = cur.fetchall()

        with c2:
            st.markdown('<div class="metric-card"><h3>📦 我的订单</h3></div>', unsafe_allow_html=True)
            if orders:
                for order in orders:
                    emoji = {"completed": "✅", "shipped": "🚚", "pending": "⏳"}.get(order["status"], "❓")
                    st.write(f"{emoji} {order['product_name']} x{order['quantity']} ¥{order['total_price']}")
            else:
                st.info("暂无订单")

        conn.close()
    except Exception as e:
        with c1:
            st.error(f"加载失败: {e}")

    with c3:
        st.markdown('<div class="metric-card"><h3>🧠 记忆系统</h3></div>', unsafe_allow_html=True)
        try:
            from app.memory.long_term import LongTermMemory
            from app.memory.md_memory import MDMemory
            
            # 向量库记忆
            memory = LongTermMemory()
            profile = memory.get_user_profile(st.session_state["user_id"])
            st.write(f"**向量库记忆条数:** {profile.get('memory_count', 0)}")
            
            # MD 文档记忆
            md_memory = MDMemory()
            md_profile = md_memory.get_user_profile(st.session_state["user_id"])
            st.write(f"**MD 文档记忆条数:** {md_profile.get('memory_count', 0)}")
            
            if md_profile.get("recent_memories"):
                st.write("**近期 MD 记忆:**")
                for m in md_profile["recent_memories"][:5]:
                    st.caption(f"- {m['content'][:80]}...")
            else:
                st.info("暂无 MD 文档记忆")
                
            with st.expander("🔍 搜索记忆"):
                search_query = st.text_input("搜索关键词", key="memory_search")
                if search_query:
                    # 搜索向量库
                    results = memory.recall(st.session_state["user_id"], search_query, k=5)
                    if results:
                        st.write("**向量库结果:**")
                        for r in results:
                            st.write(f"- {r['content'][:100]}")
                    
                    # 搜索 MD 文档
                    md_results = md_memory.search_memories(st.session_state["user_id"], search_query)
                    if md_results:
                        st.write("**MD 文档结果:**")
                        for r in md_results:
                            st.write(f"- {r['content'][:100]}")
                    
                    if not results and not md_results:
                        st.info("未找到相关记忆")
        except Exception as e:
            st.warning(f"记忆系统: {str(e)[:80]}")

    st.markdown("---")
    
    # ===== 手动压缩 =====
    st.subheader("🗜️ 手动压缩对话")
    compress_scene = st.selectbox(
        "压缩范围",
        options=["shopping", "travel", "social"],
        format_func=lambda value: {
            "shopping": "购物/客服",
            "travel": "旅行",
            "social": "社交协商",
        }[value],
        key="compress_scene",
    )
    if st.button("压缩当前对话", key="compress_conversation"):
        orch = get_orchestrator_v2()
        if orch:
            summary = orch.compress_conversation(
                st.session_state["user_id"],
                st.session_state["browser_session_id"],
                scene=compress_scene,
            )
            if summary:
                st.session_state["pending_compressed_summary"] = summary
                st.session_state["pending_compressed_scene"] = compress_scene
                st.rerun()
            else:
                st.info("没有可压缩的对话")

    pending_summary = st.session_state.get("pending_compressed_summary", "")
    if pending_summary:
        st.write("**压缩结果：**")
        st.write(pending_summary)
        save_col, discard_col = st.columns(2)
        with save_col:
            if st.button("💾 保存压缩结果", key="save_compressed"):
                _queue_sensitive_action(
                    "memory_save_summary",
                    {
                        "user_id": st.session_state["user_id"],
                        "summary": pending_summary,
                        "idempotency_key": f"memory-summary-{uuid.uuid4().hex}",
                    },
                    "保存对话压缩摘要到长期记忆",
                )
        with discard_col:
            if st.button("❌ 放弃压缩结果", key="discard_compressed"):
                st.session_state.pop("pending_compressed_summary", None)
                st.session_state.pop("pending_compressed_scene", None)
                st.rerun()
    
    st.markdown("---")
    
    # ===== MD 文档记忆管理 =====
    st.subheader("📄 MD 文档记忆管理")
    try:
        from app.memory.md_memory import MDMemory
        from app.memory.repository import get_memory_repository
        md_memory = MDMemory()
        memory_repository = get_memory_repository()
        
        # 显示所有记忆
        all_memories = md_memory.get_all_memories(st.session_state["user_id"])
        if all_memories:
            for memory in all_memories:
                with st.expander(f"[{memory['id']}] {memory['content'][:50]}..."):
                    st.write(f"**类型:** {memory['type']}")
                    st.write(f"**类别:** {memory.get('category', memory.get('event_type', 'general'))}")
                    st.write(f"**时间:** {memory['timestamp']}")
                    st.write(f"**内容:** {memory['content']}")
                    
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button(f"🗑️ 删除", key=f"delete_{memory['id']}"):
                            _queue_sensitive_action(
                                "memory_delete",
                                {
                                    "user_id": st.session_state["user_id"],
                                    "memory_id": memory["id"],
                                    "idempotency_key": f"memory-delete-{memory['id']}-{uuid.uuid4().hex}",
                                },
                                f"删除记忆 {memory['id']}",
                            )
                    with col2:
                        new_content = st.text_area(
                            "编辑内容",
                            value=memory['content'],
                            key=f"edit_{memory['id']}",
                            height=100
                        )
                        if st.button(f"💾 更新", key=f"update_{memory['id']}"):
                            _queue_sensitive_action(
                                "memory_update",
                                {
                                    "user_id": st.session_state["user_id"],
                                    "memory_id": memory["id"],
                                    "content": new_content,
                                    "idempotency_key": f"memory-update-{memory['id']}-{uuid.uuid4().hex}",
                                },
                                f"更新记忆 {memory['id']}",
                            )
        else:
            st.info("暂无 MD 文档记忆")
    except Exception as e:
        st.warning(f"MD 文档记忆: {str(e)[:80]}")

    st.markdown("---")
    st.subheader("💬 当前会话历史")
    if st.session_state["messages"]:
        col_a, col_b = st.columns([3, 1])
        with col_a:
            st.write(f"共 {len(st.session_state['messages'])} 条消息")
        with col_b:
            if st.button("🗑️ 清空会话", key="clear_msgs"):
                st.session_state["messages"] = []
                st.rerun()
        for msg in st.session_state["messages"][-10:]:
            with st.chat_message(msg["role"]):
                render_assistant_message(msg)
    else:
        st.info("暂无会话记录")

    st.markdown("---")
    st.subheader("🛍️ 商品分类浏览")
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        categories = ["全部", "服装", "户外", "电子"]
        selected_cat = st.selectbox("选择分类", categories, key="cat_filter")
        if selected_cat == "全部":
            products = conn.execute("SELECT * FROM products LIMIT 20").fetchall()
        else:
            products = conn.execute("SELECT * FROM products WHERE category=?", (selected_cat,)).fetchall()
        if products:
            cols = st.columns(3)
            for i, p in enumerate(products):
                with cols[i % 3]:
                    with st.container():
                        st.markdown(f"**{p['name']}**")
                        st.write(f"💰 ¥{p['price']} | ⭐ {p['rating']}")
                        st.caption(f"{p['brand']} | {p['description'][:40]}...")
                        if p['waterproof']:
                            st.caption("💧 防水")
        conn.close()
    except Exception as e:
        st.error(f"加载商品失败: {e}")
