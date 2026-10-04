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
import time
import threading
import queue as thread_queue

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import set_main_config, set_small_config, set_embedding_config, get_embedding_status

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
    .event-error { background: #ffebee; padding: 8px 12px; border-radius: 6px; margin: 4px 0; border-left: 3px solid #f44336; }
    .event-done { background: #f3e5f5; padding: 8px 12px; border-radius: 6px; margin: 4px 0; border-left: 3px solid #9c27b0; }
</style>
""", unsafe_allow_html=True)

# ========== 会话初始化（防御式） ==========
if "messages" not in st.session_state:
    st.session_state["messages"] = []
if "user_id" not in st.session_state:
    st.session_state["user_id"] = "user_001"
if "orchestrator_v2" not in st.session_state:
    st.session_state["orchestrator_v2"] = None
if "candidate_memories" not in st.session_state:
    st.session_state["candidate_memories"] = []
if "show_memory_extraction" not in st.session_state:
    st.session_state["show_memory_extraction"] = False
if "memory_diag" not in st.session_state:
    st.session_state["memory_diag"] = ""

# ========== Orchestrator 初始化 ==========
def get_orchestrator_v2():
    if st.session_state["orchestrator_v2"] is None:
        try:
            from app.config import get_config
            cfg = get_config()
            main_cfg = cfg.get("main", {})
            api_key = main_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                st.error("❌ 请先在左侧输入 API Key")
                return None
            from app.agents.orchestrator_v2 import get_orchestrator_v2 as _get
            st.session_state["orchestrator_v2"] = _get(model_name=None)
        except Exception as e:
            st.error(f"初始化失败: {e}")
            return None
    return st.session_state["orchestrator_v2"]

# ========== 实时流式渲染器 ==========
def run_streaming_realtime(async_gen_factory):
    """
    实时渲染 streaming 事件流
    """
    try:
        return _run_streaming_impl(async_gen_factory)
    except Exception as e:
        st.error(f"流式处理失败: {e}")
        return "", []



def _esc(s):
    """转义动态文本，安全地嵌入样式卡片 HTML。"""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_event_html(evt):
    """把单个过程事件渲染成带样式的 HTML 卡片（与模型正文明显区分）。

    - 步骤/分类/工具都用带左边框的彩色卡片（复用顶部 .event-* CSS）
    - 用 <br> 做卡片内换行，渲染时统一 unsafe_allow_html=True，不会被转义成 &lt;br&gt;
    - nl2sql 显示为「SQL 数据检索」过程卡（内嵌一行 code），不再裸奔大段代码块
    """
    et = evt.get("event", "")
    data = evt.get("data", {}) or {}
    if et == "thinking":
        return f'<div class="event-thinking">💭 {_esc(data.get("message", ""))}</div>'
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
        return (f'<div class="event-step">📋 <b>Step {step_num}/{total}: {_esc(name)}</b><br>'
                f'{_esc(desc)}</div>')
    if et == "tool_call":
        tn = data.get("tool", "")
        ti = (data.get("input", "") or "").replace("\n", " ").strip()
        if tn == "nl2sql":
            short_sql = ti if len(ti) <= 160 else ti[:160] + "…"
            return ('<div class="event-tool">🔧 <b>SQL 数据检索</b><br>'
                    '生成并执行 SQL 查询商品数据<br>'
                    f'<code>{_esc(short_sql)}</code></div>')
        if tn == "rag":
            return f'<div class="event-tool">📚 <b>RAG 评价检索</b><br>{_esc(ti)}</div>'
        return f'<div class="event-tool">⚙️ <b>{_esc(tn)}</b><br>{_esc(ti)}</div>'
    if et == "error":
        return f'<div class="event-error">❌ <b>错误</b>: {_esc(data.get("message", ""))}</div>'
    return ""


def render_process_events(events):
    """把一组过程事件按顺序渲染成样式卡片（步骤在前、与正文区分）。"""
    cards = []
    for evt in events:
        h = _fmt_event_html(evt)
        if h:
            cards.append(h)
    if cards:
        st.markdown("".join(cards), unsafe_allow_html=True)


def render_assistant_message(msg):
    """渲染一条助手消息：先过程步骤卡片，再模型回复正文（正文无卡片、明显不同）。

    兼容新结构 {"process_events": [...], "response": "..."} 与旧结构 {"content": "..."}。
    """
    if isinstance(msg.get("process_events"), list) or "response" in msg:
        render_process_events(msg.get("process_events", []))
        resp = msg.get("response", "")
        if resp:
            st.markdown(resp)
    else:
        st.markdown(msg.get("content", ""))


def _run_streaming_impl(async_gen_factory):
    """内部实现，被 run_streaming_realtime 包装。

    关键设计：
    - 每个过程事件渲染成一张样式卡片，逐个追加、永久保留、按顺序出现。
    - 「正在思考」占位立即显示，避免发送后长时间空白。
    - 回复容器延迟到第一个 token 才创建，保证回复排在所有步骤卡片之后（顺序不再颠倒）。
    - 卡片间极短停顿，让步骤按顺序“走出来”，不会瞬时挤成一坨。
    """
    event_q = thread_queue.Queue()
    all_events = []
    token_buffer = []
    done_response = ""
    stop_event = threading.Event()

    def _bg_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        async def _drain():
            try:
                gen = async_gen_factory()
                async for sse_line in gen:
                    if stop_event.is_set():
                        break
                    if sse_line.startswith("data: "):
                        try:
                            event_q.put(json.loads(sse_line[6:]))
                        except json.JSONDecodeError:
                            pass
                event_q.put(None)
            except Exception as e:
                event_q.put({"event": "error", "data": {"message": str(e)}, "step": ""})
                event_q.put(None)
        loop.run_until_complete(_drain())
        loop.close()

    bg = threading.Thread(target=_bg_worker, daemon=True)
    bg.start()

    # 立即显示“正在思考”占位，避免空白
    loading = st.empty()
    loading.markdown("⏳ 正在思考，正在规划处理步骤…")

    # 延迟创建：保证回复出现在步骤卡片之后
    response_container = None
    first_event = True

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

            if event_type == "token":
                token = data.get("token", "")
                token_buffer.append(token)
                if response_container is None:
                    response_container = st.empty()
                response_container.markdown("".join(token_buffer))
                continue

            if event_type == "done":
                done_response = data.get("response", "")
                if not token_buffer and done_response:
                    if response_container is None:
                        response_container = st.empty()
                    response_container.markdown(done_response)
                continue

            card_html = _fmt_event_html(evt)
            if card_html:
                st.markdown(card_html, unsafe_allow_html=True)
                time.sleep(0.02)
    finally:
        stop_event.set()

    if first_event:
        loading.empty()

    final_response = "".join(token_buffer) if token_buffer else done_response
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
        from app.memory.md_memory import MDMemory
        md_memory = MDMemory()
        user_dir = md_memory._get_user_dir(st.session_state["user_id"])
        if os.path.exists(user_dir):
            import shutil
            shutil.rmtree(user_dir)
            st.success("已清空所有 MD 文档记忆")
            st.rerun()

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

    # 2. 记忆提取面板（在模型输出之后、输入框之前）
    if st.session_state.get("show_memory_extraction"):
        st.subheader("📝 记忆提取结果")
        with st.expander("🔍 提取诊断（验证小模型是否执行）"):
            st.code(st.session_state.get("memory_diag", "无诊断信息"))
        _orch = get_orchestrator_v2()
        candidates = st.session_state.get("candidate_memories", [])
        if not candidates:
            st.info("小模型本次未提取到值得保存的记忆。")
            if st.button("👌 知道了", key="dismiss_empty"):
                st.session_state["show_memory_extraction"] = False
                st.rerun()
        selected_indices = []
        for i, candidate in enumerate(candidates):
            emoji = {"shopping": "🛍️", "travel": "✈️", "general": "📝"}.get(candidate["category"], "📝")
            confidence = {"high": "高", "medium": "中", "low": "低"}.get(candidate["confidence"], "中")
            col1, col2 = st.columns([4, 1])
            with col1:
                st.write(f"{i+1}. {emoji} {candidate['content']} (置信度: {confidence})")
            with col2:
                if st.checkbox("保存", key=f"memory_{i}", value=False):
                    selected_indices.append(i)
        if candidates:
            col1, col2, col3 = st.columns(3)
            with col1:
                if st.button("✅ 确认写入选中", key="save_selected_memories"):
                    if selected_indices and _orch:
                        saved_ids = _orch.save_user_selected_memories(st.session_state["user_id"], candidates, selected_indices)
                        st.success(f"已保存 {len(saved_ids)} 条记忆")
                        st.session_state["show_memory_extraction"] = False
                        st.session_state["candidate_memories"] = []
                        st.rerun()
                    else:
                        st.warning("请先勾选要保存的记忆")
            with col2:
                if st.button("❌ 拒绝写入", key="skip_memories"):
                    st.session_state["show_memory_extraction"] = False
                    st.session_state["candidate_memories"] = []
                    st.rerun()
            with col3:
                if st.button("💾 全部写入", key="save_all_memories"):
                    if _orch:
                        all_indices = list(range(len(candidates)))
                        saved_ids = _orch.save_user_selected_memories(st.session_state["user_id"], candidates, all_indices)
                        st.success(f"已保存 {len(saved_ids)} 条记忆")
                        st.session_state["show_memory_extraction"] = False
                        st.session_state["candidate_memories"] = []
                        st.rerun()

    # 3. 处理待处理的新消息（渲染响应 + 提取记忆）
    if st.session_state.get("pending_shopping"):
        prompt = st.session_state.pop("pending_shopping")
        st.session_state["shopping_msgs"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            orch = get_orchestrator_v2()
            _uid = st.session_state["user_id"]
            if orch:
                async def _gen():
                    async for line in orch.process_streaming(prompt, _uid):
                        yield line
                response, _events = run_streaming_realtime(_gen)
            else:
                response = "请先在左侧配置 API Key"
                _events = []
                st.warning(response)
            # Step 4/4 过程卡（在气泡内、归入步骤组）
            _events.append({"event": "step", "data": {
                "step": 4, "total": 4,
                "name": "记忆提取",
                "description": "小模型分析本次对话，提取值得保存的记忆"
            }})
            st.markdown(_fmt_event_html(_events[-1]), unsafe_allow_html=True)
        st.session_state["shopping_msgs"].append({
            "role": "assistant", "process_events": _events, "response": response
        })
        # 对话结束后提取候选记忆（Step 4/4）
        if orch:
            candidates, diag = orch.extract_candidate_memories_with_diag(_uid, "default")
            st.session_state["candidate_memories"] = candidates
            st.session_state["memory_diag"] = diag
            st.session_state["show_memory_extraction"] = True
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

    # 2. 记忆提取面板（在模型输出之后、输入框之前）
    if st.session_state.get("show_memory_extraction"):
        st.subheader("📝 记忆提取结果")
        with st.expander("🔍 提取诊断（验证小模型是否执行）"):
            st.code(st.session_state.get("memory_diag", "无诊断信息"))
        _orch = get_orchestrator_v2()
        candidates = st.session_state.get("candidate_memories", [])
        if not candidates:
            st.info("小模型本次未提取到值得保存的记忆。")
            if st.button("👌 知道了", key="dismiss_empty_travel"):
                st.session_state["show_memory_extraction"] = False
                st.rerun()
        selected_indices = []
        for i, candidate in enumerate(candidates):
            emoji = {"shopping": "🛍️", "travel": "✈️", "general": "📝"}.get(candidate["category"], "📝")
            confidence = {"high": "高", "medium": "中", "low": "低"}.get(candidate["confidence"], "中")
            col1, col2 = st.columns([4, 1])
            with col1:
                st.write(f"{i+1}. {emoji} {candidate['content']} (置信度: {confidence})")
            with col2:
                if st.checkbox("保存", key=f"memory_travel_{i}", value=False):
                    selected_indices.append(i)
        if candidates:
            col1, col2, col3 = st.columns(3)
            with col1:
                if st.button("✅ 确认写入选中", key="save_selected_memories_travel"):
                    if selected_indices and _orch:
                        saved_ids = _orch.save_user_selected_memories(st.session_state["user_id"], candidates, selected_indices)
                        st.success(f"已保存 {len(saved_ids)} 条记忆")
                        st.session_state["show_memory_extraction"] = False
                        st.session_state["candidate_memories"] = []
                        st.rerun()
                    else:
                        st.warning("请先勾选要保存的记忆")
            with col2:
                if st.button("❌ 拒绝写入", key="skip_memories_travel"):
                    st.session_state["show_memory_extraction"] = False
                    st.session_state["candidate_memories"] = []
                    st.rerun()
            with col3:
                if st.button("💾 全部写入", key="save_all_memories_travel"):
                    if _orch:
                        all_indices = list(range(len(candidates)))
                        saved_ids = _orch.save_user_selected_memories(st.session_state["user_id"], candidates, all_indices)
                        st.success(f"已保存 {len(saved_ids)} 条记忆")
                        st.session_state["show_memory_extraction"] = False
                        st.session_state["candidate_memories"] = []
                        st.rerun()

    # 3. 处理待处理的新消息（渲染响应 + 提取记忆）
    if st.session_state.get("pending_travel"):
        prompt = st.session_state.pop("pending_travel")
        st.session_state["travel_msgs"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            orch = get_orchestrator_v2()
            _uid = st.session_state["user_id"]
            if orch:
                async def _gen():
                    async for line in orch.process_streaming(prompt, _uid):
                        yield line
                response, _events = run_streaming_realtime(_gen)
            else:
                response = "请先在左侧配置 API Key"
                _events = []
                st.warning(response)
            # Step 4/4 过程卡（在气泡内、归入步骤组）
            _events.append({"event": "step", "data": {
                "step": 4, "total": 4,
                "name": "记忆提取",
                "description": "小模型分析本次对话，提取值得保存的记忆"
            }})
            st.markdown(_fmt_event_html(_events[-1]), unsafe_allow_html=True)
        st.session_state["travel_msgs"].append({
            "role": "assistant", "process_events": _events, "response": response
        })
        # 对话结束后提取候选记忆（Step 4/4）
        if orch:
            candidates, diag = orch.extract_candidate_memories_with_diag(_uid, "default")
            st.session_state["candidate_memories"] = candidates
            st.session_state["memory_diag"] = diag
            st.session_state["show_memory_extraction"] = True
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
                    async for line in orch.process_streaming(prompt, _uid):
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
    if st.button("压缩当前对话", key="compress_conversation"):
        orch = get_orchestrator_v2()
        if orch:
            summary = orch.compress_conversation(st.session_state["user_id"], "default")
            if summary:
                st.write("**压缩结果：**")
                st.write(summary)
                
                if st.button("💾 保存压缩结果", key="save_compressed"):
                    memory_id = orch.save_compressed_summary(st.session_state["user_id"], summary)
                    st.success(f"已保存压缩结果，ID: {memory_id}")
                    st.rerun()
            else:
                st.info("没有可压缩的对话")
    
    st.markdown("---")
    
    # ===== MD 文档记忆管理 =====
    st.subheader("📄 MD 文档记忆管理")
    try:
        from app.memory.md_memory import MDMemory
        md_memory = MDMemory()
        
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
                            md_memory.delete_memory(st.session_state["user_id"], memory['id'])
                            st.success("已删除")
                            st.rerun()
                    with col2:
                        new_content = st.text_area(
                            "编辑内容",
                            value=memory['content'],
                            key=f"edit_{memory['id']}",
                            height=100
                        )
                        if st.button(f"💾 更新", key=f"update_{memory['id']}"):
                            md_memory.update_memory(st.session_state["user_id"], memory['id'], new_content)
                            st.success("已更新")
                            st.rerun()
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
