"""
SmartLife Agent v2 - Streaming 前端应用
全链路实时展示：分类 → 路由 → 工具调用 → 流式生成
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


def _run_streaming_impl(async_gen_factory):
    """内部实现，被 run_streaming_realtime 包装"""
    event_q = thread_queue.Queue()
    all_events = []
    token_buffer = []
    stop_event = threading.Event()

    # 后台线程
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
                event_q.put(None)  # 结束信号
            except Exception as e:
                event_q.put({"event": "error", "data": {"message": str(e)}, "step": ""})
                event_q.put(None)
        loop.run_until_complete(_drain())
        loop.close()

    bg = threading.Thread(target=_bg_worker, daemon=True)
    bg.start()

    # 创建 Streamlit 容器
    thinking_container = st.empty()
    step_container = st.empty()
    tool_container = st.empty()
    response_container = st.empty()
    done_container = st.empty()

    step_lines = []
    tool_lines = []
    current_thinking = ""

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
            data = evt.get("data", {})
            step = evt.get("step", "")

            if event_type == "thinking":
                current_thinking = data.get("message", "")
                thinking_container.markdown(f'<div class="event-thinking">💭 {current_thinking}</div>', unsafe_allow_html=True)

            elif event_type == "classify":
                info = data
                step_lines.append(f"🔍 分类: **{info.get('category','')}** | 路由: **{info.get('route','')}** | 意图: **{info.get('intent','')}**")
                with step_container.container():
                    for line in step_lines:
                        st.markdown(f'<div class="event-classify">{line}</div>', unsafe_allow_html=True)

            elif event_type == "tool_call":
                tool_name = data.get("tool", "unknown")
                tool_input = data.get("input", {})
                tool_lines.append(f"🔧 调用: **{tool_name}** `{json.dumps(tool_input, ensure_ascii=False)[:80]}`")
                with tool_container.container():
                    for line in tool_lines:
                        st.markdown(f'<div class="event-tool">{line}</div>', unsafe_allow_html=True)

            elif event_type == "step":
                step_lines.append(f"📌 {data.get('name', '')} - {data.get('description', '')}")
                with step_container.container():
                    for line in step_lines:
                        st.markdown(f'<div class="event-classify">{line}</div>', unsafe_allow_html=True)

            elif event_type == "token":
                token_buffer.append(data.get("token", ""))
                response_container.markdown("".join(token_buffer))

            elif event_type == "done":
                final = data.get("content", data.get("token", ""))
                if final and not token_buffer:
                    token_buffer.append(final)
                    response_container.markdown("".join(token_buffer))
                done_container.markdown(f'<div class="event-done">✅ 完成</div>', unsafe_allow_html=True)

            elif event_type == "error":
                st.error(f"❌ {data.get('message', '未知错误')}")

    except Exception as e:
        st.error(f"渲染异常: {e}")

    return "".join(token_buffer), all_events


def run_streaming_simple(async_gen_factory):
    """简单版：只显示最终文本"""
    try:
        return _run_streaming_impl(async_gen_factory)
    except Exception as e:
        st.error(f"处理失败: {e}")
        return "", []


# ========== 侧边栏 ==========
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

    # 配置状态提示
    emb_status, emb_msg = get_embedding_status()
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
    st.subheader("📖 架构说明")
    st.markdown("""
    **Streaming 全链路：**
    1. ⚡ 小模型分类（路由+意图+检索）
    2. 🔧 智能检索（SQL/RAG/混合）
    3. 🧠 大模型流式生成
    4. 📝 记忆更新

    **分类策略：**
    - 关键词匹配 → 零延迟
    - 小模型判断 → 处理模糊场景
    - 合并路由+意图 → 一次调用
    """)

# ========== 主界面 ==========
st.markdown('<h1 class="main-header">SmartLife Agent v2</h1>', unsafe_allow_html=True)
st.caption("全链路 Streaming · 实时展示思考过程 · 智能检索路由")

tab1, tab2, tab3, tab4 = st.tabs(["🛒 购物场景", "✈️ 旅游场景", "🤝 社交协商", "👤 个人中心"])

# ===== Tab1: 购物场景 (Streaming) =====
with tab1:
    st.subheader("🛒 智能购物助手")

    if "shopping_msgs" not in st.session_state:
        st.session_state["shopping_msgs"] = []

    for msg in st.session_state["shopping_msgs"][-6:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("描述你的购物需求...", key="shopping_input"):
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
                response, _ = run_streaming_realtime(_gen)
            else:
                response = "请先在左侧配置 API Key"
                st.warning(response)

        st.session_state["shopping_msgs"].append({"role": "assistant", "content": response})

# ===== Tab2: 旅游场景 =====
with tab2:
    st.subheader("✈️ 智能旅游助手")

    if "travel_msgs" not in st.session_state:
        st.session_state["travel_msgs"] = []

    for msg in st.session_state["travel_msgs"][-6:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("描述你的旅行计划...", key="travel_input"):
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
                response, _ = run_streaming_realtime(_gen)
            else:
                response = "请先在左侧配置 API Key"
                st.warning(response)

        st.session_state["travel_msgs"].append({"role": "assistant", "content": response})

# ===== Tab3: 社交协商 =====
with tab3:
    st.subheader("🤝 社交协商模拟")

    if "social_msgs" not in st.session_state:
        st.session_state["social_msgs"] = []

    for msg in st.session_state["social_msgs"][-6:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

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
                st.info(f"未找到用户 {st.session_state["user_id"]}")

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
            memory = LongTermMemory()
            profile = memory.get_user_profile(st.session_state["user_id"])
            st.write(f"**记忆条数:** {profile.get('memory_count', 0)}")
            if profile.get("recent_memories"):
                st.write("**近期记忆:**")
                for m in profile["recent_memories"][:5]:
                    st.caption(f"- {m['content'][:80]}...")
            else:
                st.info("暂无记忆")
            with st.expander("🔍 搜索记忆"):
                search_query = st.text_input("搜索关键词", key="memory_search")
                if search_query:
                    results = memory.recall(st.session_state["user_id"], search_query, k=5)
                    if results:
                        for r in results:
                            st.write(f"- {r['content'][:100]}")
                    else:
                        st.info("未找到相关记忆")
        except Exception as e:
            st.warning(f"记忆系统: {str(e)[:80]}")

    st.markdown("---")
    st.subheader("💬 当前会话历史")
    if st.session_state["messages"]:
        col_a, col_b = st.columns([3, 1])
        with col_a:
            st.write(f"共 {len(st.session_state["messages"])} 条消息")
        with col_b:
            if st.button("🗑️ 清空会话", key="clear_msgs"):
                st.session_state["messages"] = []
                st.rerun()
        for msg in st.session_state["messages"][-10:]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
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
