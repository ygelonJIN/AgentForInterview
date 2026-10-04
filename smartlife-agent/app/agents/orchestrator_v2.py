"""
Orchestrator v2 - 全链路 Streaming 主协调器

核心改动：
1. 统一分类器替代 Router + Intent 双调用
2. 所有 LLM 调用使用 astream() 替代 invoke()
3. 全程通过 EventQueue 推送实时事件
4. 检索策略按分类结果智能路由
5. 复用子 Agent 的 streaming 方法，不内联重复逻辑
"""
import sys
import os
import json
import re
import asyncio
from typing import Dict, List, Any, Optional, AsyncGenerator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from app.config import create_llm
from app.classifier import UnifiedClassifier, ClassificationResult, get_classifier
from app.streaming import EventQueue, EventType
from app.agents.shopping_agent import ShoppingAgent
from app.agents.travel_agent import TravelAgent
from app.agents.negotiation_agent import NegotiationAgent
from app.negotiation.graph import NegotiationGraph
from app.memory.short_term import ShortTermMemory
from app.memory.long_term import LongTermMemory
from app.memory.compressor import MemoryCompressor


class OrchestratorV2:
    """
    主协调器 v2 - 全链路流式处理

    流程：
    1. 统一分类（小模型，一次调用）→ 路由 + 意图 + 检索策略
    2. 根据意图路由到子 Agent
    3. 子 Agent 流式输出（astream）
    4. 全程事件推送
    """

    def __init__(self, model_name: str = None):
        self.llm = create_llm(model_name)
        self.classifier = get_classifier()

        # 子 Agents
        self.shopping_agent = ShoppingAgent(model_name)
        self.travel_agent = TravelAgent(model_name)
        self.negotiation_agent = NegotiationAgent()
        self.negotiation_graph = NegotiationGraph()

        # 记忆
        self._short_term_memory = None
        self._long_term_memory = None
        self._compressor = None

        # 通用对话 prompt
        self.general_prompt = ChatPromptTemplate.from_messages([
            ("system", "你是 SmartLife Agent 智能助手，帮助用户购物和旅行规划。用中文回复。友好、专业、简洁。"),
            MessagesPlaceholder(variable_name="chat_history"),
            ("user", "{input}")
        ])

    @property
    def short_term_memory(self):
        if self._short_term_memory is None:
            self._short_term_memory = ShortTermMemory()
        return self._short_term_memory

    @property
    def long_term_memory(self):
        if self._long_term_memory is None:
            try:
                self._long_term_memory = LongTermMemory()
            except Exception:
                pass
        return self._long_term_memory

    @property
    def compressor(self):
        if self._compressor is None:
            try:
                self._compressor = MemoryCompressor()
            except Exception:
                pass
        return self._compressor

    # ================================================================
    # Streaming 入口
    # ================================================================

    async def process_streaming(
        self,
        user_message: str,
        user_id: str = "user_001",
        session_id: str = "default"
    ) -> AsyncGenerator[str, None]:
        """
        全链路流式处理 - 返回 SSE 事件流

        Yields:
            SSE 格式的事件字符串
        """
        queue = EventQueue()

        async def _process():
            try:
                await self._do_process(user_message, user_id, session_id, queue)
            except Exception as e:
                await queue.emit_error(f"处理失败: {str(e)}")
            finally:
                await queue.finish()

        task = asyncio.create_task(_process())
        async for sse in queue.to_sse():
            yield sse
        await task

    async def process_negotiation_streaming(
        self,
        participants: List[Dict[str, Any]],
        scenario: str = "travel"
    ) -> AsyncGenerator[str, None]:
        """
        协商场景流式处理

        Args:
            participants: 参与者列表
            scenario: 场景类型

        Yields:
            SSE 格式的事件字符串
        """
        queue = EventQueue()

        async def _process():
            try:
                await self._do_negotiation(participants, scenario, queue)
            except Exception as e:
                await queue.emit_error(f"协商失败: {str(e)}")
            finally:
                await queue.finish()

        task = asyncio.create_task(_process())
        async for sse in queue.to_sse():
            yield sse
        await task

    # ================================================================
    # 核心处理逻辑
    # ================================================================

    async def _do_process(
        self,
        user_message: str,
        user_id: str,
        session_id: str,
        queue: EventQueue
    ):
        """核心处理逻辑"""

        # Step 1: 保存到短期记忆
        self.short_term_memory.add_message(session_id, "user", user_message)

        # Step 2: 统一分类（小模型，一次调用同时输出路由+意图+检索策略）
        await queue.emit(EventType.STEP, {
            "step": 1, "total": 4,
            "name": "统一分类",
            "description": "小模型分析任务类型和路由策略"
        }, step="classify")

        classification = await self.classifier.aclassify_streaming(user_message, queue)

        # Step 3: 获取聊天历史
        chat_history = self.short_term_memory.get_langchain_messages(session_id, last_n=10)

        # Step 3.5: 安全网 - 如果分类为 general 但包含产品关键词，强制改为 shopping
        if classification.intent == "general":
            product_keywords = ["鞋", "衣服", "手机", "电脑", "背包", "帐篷", "冲锋衣",
                               "睡袋", "泳衣", "T恤", "衬衫", "裤子", "外套", "连衣裙",
                               "元", "块", "预算", "价格", "多少钱", "推荐"]
            if any(kw in user_message for kw in product_keywords):
                classification.intent = "shopping"
                classification.sub_intent = "search"
                classification.retrieval = "mixed"
                await queue.emit(EventType.CLASSIFY, {
                    "route": classification.route,
                    "intent": "shopping",
                    "sub_intent": "search",
                    "retrieval": "mixed",
                    "confidence": 0.8,
                    "reason": "安全网：检测到产品关键词，强制路由到购物",
                    "method": "safety_net"
                }, step="classify")

        # Step 4: 根据意图路由到子 Agent（流式）
        await queue.emit(EventType.STEP, {
            "step": 2, "total": 4,
            "name": "Agent执行",
            "description": f"路由到 {classification.intent} Agent"
        }, step="agent")

        response_text = ""

        if classification.intent in ("shopping", "customer_service"):
            response_text = await self._run_shopping_streaming(
                user_message, user_id, chat_history, classification, queue
            )

        elif classification.intent == "travel":
            if classification.route == "plan_and_execute":
                response_text = await self.travel_agent.plan_trip_streaming(
                    user_message, user_id, queue
                )
            else:
                response_text = await self.travel_agent.chat_streaming(
                    user_message, user_id, queue
                )

        elif classification.intent == "negotiation":
            response_text = "请在「社交协商」标签页中配置参与者信息后开始协商。"
            await queue.emit_token(response_text, step="agent")

        else:
            response_text = await self._run_general_streaming(
                user_message, session_id, chat_history, queue
            )

        # Step 5: 保存回复到记忆
        await queue.emit(EventType.STEP, {
            "step": 3, "total": 4,
            "name": "记忆更新",
            "description": "保存对话到记忆系统"
        }, step="memory")

        self.short_term_memory.add_message(session_id, "assistant", response_text)
        self._save_memories(user_id, session_id, classification.intent, user_message)

        # Step 6: 完成
        await queue.emit_done({
            "agent": classification.intent,
            "route": classification.route,
            "retrieval": classification.retrieval,
            "confidence": classification.confidence,
            "response_length": len(response_text)
        })

    async def _do_negotiation(
        self,
        participants: List[Dict[str, Any]],
        scenario: str,
        queue: EventQueue
    ):
        """协商处理逻辑 - 带 streaming 事件"""

        # Step 1: 偏好分析
        await queue.emit(EventType.STEP, {
            "step": 1, "total": 4,
            "name": "偏好分析",
            "description": f"分析 {len(participants)} 位参与者的偏好"
        }, step="analyze")
        await queue.emit_thinking("🧠 正在分析每位参与者的偏好...", step="analyze")

        # 补充记忆数据
        enriched_participants = []
        for p in participants:
            user_id = p.get("user_id", "unknown")
            memories = []
            if self.long_term_memory:
                memories = self.long_term_memory.get_cross_scene_memories(user_id, scenario)
            enriched = {
                **p,
                "purchase_history": [m["content"] for m in memories if "购物" in m.get("content", "") or "购买" in m.get("content", "")],
                "travel_history": [m["content"] for m in memories if "旅行" in m.get("content", "") or "景点" in m.get("content", "")]
            }
            enriched_participants.append(enriched)

        for p in enriched_participants:
            uid = p.get("user_id", "?")
            prefs = p.get("preferences", {})
            style = prefs.get("style", "?")
            budget = prefs.get("budget", "?")
            await queue.emit(EventType.TOOL_RESULT, {
                "tool": "偏好分析",
                "output": f"{uid}: 风格={style}, 预算={budget}"
            }, step="analyze")

        # Step 2: 找共同点
        await queue.emit(EventType.STEP, {
            "step": 2, "total": 4,
            "name": "寻找共同点",
            "description": "分析参与者之间的共同偏好"
        }, step="common")
        await queue.emit_thinking("🧠 正在寻找共同点...", step="common")

        # Step 3: 识别冲突
        await queue.emit(EventType.STEP, {
            "step": 3, "total": 4,
            "name": "冲突识别",
            "description": "识别偏好冲突并制定解决方案"
        }, step="conflict")
        await queue.emit_thinking("🧠 正在识别冲突并协商...", step="conflict")

        # 执行协商（同步，在线程池中运行）
        result = await asyncio.to_thread(
            self.negotiation_graph.negotiate, enriched_participants
        )

        # 推送协商结果事件
        conflicts = result.get("conflicts", [])
        if conflicts:
            for c in conflicts:
                await queue.emit(EventType.TOOL_RESULT, {
                    "tool": "冲突识别",
                    "output": f"[{c.get('type', '?')}] {c.get('description', '?')}"
                }, step="conflict")

        common = result.get("common_ground", {})
        if common.get("common_tags"):
            await queue.emit(EventType.TOOL_RESULT, {
                "tool": "共同点",
                "output": f"共同标签: {', '.join(common['common_tags'])}"
            }, step="common")

        # Step 4: 生成方案
        await queue.emit(EventType.STEP, {
            "step": 4, "total": 4,
            "name": "生成方案",
            "description": "综合分析结果，生成协商方案"
        }, step="plan")

        # 用大模型生成自然语言方案摘要
        compromise = result.get("compromise", {})
        summary_prompt = ChatPromptTemplate.from_messages([
            ("system", "你是协商助手。根据协商结果，用友好自然的语言总结方案。用中文回复。"),
            ("user", "参与者：{participants}\n共同点：{common}\n冲突：{conflicts}\n妥协方案：{compromise}\n\n请用自然语言总结协商结果和推荐方案。")
        ])

        chain = summary_prompt | self.llm
        full_summary = ""

        # 构建参与者摘要
        p_summary = []
        for p in participants:
            prefs = p.get("preferences", {})
            p_summary.append(f"{p.get('user_id', '?')}: 风格={prefs.get('style', '?')}, 预算={prefs.get('budget', '?')}")

        async for chunk in chain.astream({
            "participants": "; ".join(p_summary),
            "common": json.dumps(common, ensure_ascii=False),
            "conflicts": json.dumps(conflicts, ensure_ascii=False),
            "compromise": json.dumps(compromise, ensure_ascii=False)
        }):
            if chunk.content:
                full_summary += chunk.content
                await queue.emit_token(chunk.content, step="plan")

        # 保存协商结果到记忆
        for p in participants:
            if self.long_term_memory:
                self.long_term_memory.save_event(
                    p.get("user_id", "unknown"),
                    event_type="negotiation",
                    event_data={
                        "action": "参与协商",
                        "object": scenario,
                        "result": "成功" if result.get("status") == "completed" else "进行中"
                    }
                )

        await queue.emit_done({
            "agent": "negotiation",
            "participants": len(participants),
            "conflicts": len(conflicts),
            "status": result.get("status", "unknown")
        })

    # ================================================================
    # 子 Agent 流式调用
    # ================================================================

    async def _run_shopping_streaming(
        self,
        user_message: str,
        user_id: str,
        chat_history: List,
        classification: ClassificationResult,
        queue: EventQueue
    ) -> str:
        """购物/客服场景 - 流式处理"""

        # 检索规划阶段：先判断真实数据需求，再选择策略
        needs = self._classify_data_needs(user_message, classification)
        planned_strategy = needs['strategy']

        await queue.emit(EventType.STEP, {
            "step": 2, "total": 4,
            "name": "数据检索",
            "description": f"策略: {planned_strategy} | review={needs['needs_review']} | product={needs['needs_product']}"
        }, step="retrieval")

        await self._emit_retrieval_plan(queue, planned_strategy, needs, source='planner')

        retrieval = await self._smart_retrieve(user_message, planned_strategy, queue, needs=needs)

        # 跨场景记忆
        cross_memories = []
        if self.long_term_memory:
            cross_memories = self.long_term_memory.get_cross_scene_memories(user_id, "shopping")
        memory_context = ""
        if cross_memories:
            memory_context = "\n用户历史偏好：\n" + "\n".join(
                [m["content"] for m in cross_memories[:3]]
            )

        full_input = user_message
        if retrieval['product_context']:
            full_input += f"\n\n【商品信息】\n{retrieval['product_context']}"
        if retrieval['review_context']:
            full_input += f"\n\n【评价/口碑信息】\n{retrieval['review_context']}"
        if retrieval['diagnostics']:
            diag_text = '\n'.join([f'- {d}' for d in retrieval['diagnostics']])
            full_input += f"\n\n【检索诊断信息】\n{diag_text}"
        if memory_context:
            full_input += f"\n\n【用户历史偏好】\n{memory_context}"

        # 流式生成回复
        await queue.emit_thinking("🧠 正在生成购物建议...", step="generate")

        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个智能导购助手，帮用户找到最合适的商品。

【重要规则】你必须且只能推荐【商品信息】中列出的商品。绝对不要编造商品名、价格或任何数据。如果没有商品信息，就告诉用户"暂未找到符合条件的商品"并建议放宽条件。

【评价规则】
- 如果提供【评价/口碑信息】，应优先引用真实评价；
- 如果【检索诊断信息】提示评价检索失败或降级，必须明确告知用户当前评价信息不完整，并说明原因；
- 绝对不能编造评价内容或伪造用户口碑。

回复要求：
- 用中文回复
- 只推荐【商品信息】中的真实商品，给出商品名、价格、品牌
- 如果有评价信息，引用关键评价
- 预算敏感时优先推荐性价比高的商品
- 结束时说明"以上商品信息来自平台数据库"""),
            MessagesPlaceholder(variable_name="chat_history"),
            ("user", "{input}")
        ])

        chain = prompt | self.llm
        full_response = ""

        async for chunk in chain.astream({
            "chat_history": chat_history,
            "input": full_input
        }):
            if chunk.content:
                full_response += chunk.content
                await queue.emit_token(chunk.content, step="generate")

        return full_response



    def _classify_data_needs(self, query: str, classification: 'ClassificationResult') -> dict:
        """基于查询语义与分类结果推导数据需求，避免仅依赖关键词选择 RAG/SQL"""
        q = (query or '').lower()
        review_signals = sum(1 for kw in ['评价', '口碑', '好不好', '差评', '好评', '吐槽', '怎么样', '耐用', '质量', '体验'] if kw in q)
        attribute_signals = sum(1 for kw in ['价格', '多少钱', '元', '预算', '库存', '品牌', '类别', '分类', '排序', '防水'] if kw in q)
        product_signals = sum(1 for kw in ['推荐', '找', '买', '搜', '商品', '哪款', '哪双', '哪个', '对比', '比较'] if kw in q)

        needs_review = False
        needs_product = False

        if classification.intent in ('shopping', 'customer_service'):
            needs_product = True
            if classification.sub_intent in ('review', 'qa', 'service') or review_signals >= 1:
                needs_review = True
            if review_signals >= 2 and attribute_signals == 0 and product_signals == 0:
                needs_review = True
                needs_product = False
        elif classification.intent == 'travel':
            needs_review = True
            needs_product = False

        strategy = classification.retrieval
        if needs_review and needs_product:
            strategy = 'mixed'
        elif needs_review:
            strategy = 'rag_only'
        elif needs_product:
            strategy = 'sql_only'

        return {
            'needs_review': needs_review,
            'needs_product': needs_product,
            'strategy': strategy,
            'signals': {
                'review': review_signals,
                'attribute': attribute_signals,
                'product': product_signals
            }
        }

    async def _emit_retrieval_plan(self, queue: 'EventQueue', strategy: str, needs: dict, source: str = 'policy'):
        await queue.emit(EventType.CLASSIFY, {
            'route': 'retrieval_plan',
            'intent': 'shopping',
            'sub_intent': 'retrieval_planning',
            'retrieval': strategy,
            'confidence': 0.9,
            'reason': f'数据需求: review={needs.get("needs_review")}, product={needs.get("needs_product")} (来源: {source})',
            'needs': needs,
            'method': 'retrieval_planner'
        }, step='classify')

    def _extract_review_keywords(self, query: str) -> list[str]:
        stopwords = {'评价', '口碑', '好不好', '差评', '好评', '吐槽', '怎么样', '耐用', '质量', '体验', '请问', '一下', '这个', '那个', '吗', '呢', '哈'}
        # For Chinese: extract 2-char bigrams and individual meaningful chars
        raw = re.findall(r'[\w]+', query)
        keywords = []
        for token in raw:
            if len(token) >= 2 and token not in stopwords:
                keywords.append(token)
            # Also add 2-char bigrams for Chinese text
            if len(token) >= 3 and all(ord(c) > 127 for c in token):
                for j in range(len(token) - 1):
                    bigram = token[j:j+2]
                    if bigram not in stopwords and bigram not in keywords:
                        keywords.append(bigram)
        seen = []
        for t in keywords:
            if t not in seen:
                seen.append(t)
        return seen[:8]

    def _direct_review_search(self, query: str, limit: int = 8) -> list[dict]:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'products.db')
        if not os.path.exists(db_path):
            return []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            keywords = self._extract_review_keywords(query)
            params = []
            if keywords:
                conds = ' OR '.join(['content LIKE ?' for _ in keywords])
                sql = f"SELECT r.*, p.name AS product_name FROM reviews r LEFT JOIN products p ON p.id = r.product_id WHERE {conds} ORDER BY r.rating DESC, r.created_at DESC LIMIT ?"
                params.extend([f'%{kw}%' for kw in keywords])
                params.append(limit)
            else:
                sql = "SELECT r.*, p.name AS product_name FROM reviews r LEFT JOIN products p ON p.id = r.product_id ORDER BY r.rating DESC, r.created_at DESC LIMIT ?"
                params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _fallback_text_review_search(self, query: str, limit: int = 8) -> list[str]:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        data_dir = os.path.join(base_dir, 'data', 'reviews')
        keywords = self._extract_review_keywords(query)
        if not os.path.isdir(data_dir):
            return []
        results = []
        for file_name in os.listdir(data_dir):
            if not file_name.endswith('.txt'):
                continue
            path = os.path.join(data_dir, file_name)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        hits = sum(1 for kw in keywords if kw in line) if keywords else 0
                        if hits or not keywords:
                            results.append((hits, os.path.basename(path), line))
            except Exception:
                continue
        results.sort(key=lambda x: (-x[0], x[1]))
        merged = []
        for _, src, line in results[:limit]:
            merged.append(f'[{src}] {line[:200]}')
        return merged


    async def _run_general_streaming(

        self,
        user_message: str,
        session_id: str,
        chat_history: List,
        queue: EventQueue
    ) -> str:
        """通用对话 - 流式处理"""
        await queue.emit_thinking("🧠 正在思考...", step="generate")

        chain = self.general_prompt | self.llm
        full_response = ""

        async for chunk in chain.astream({
            "chat_history": chat_history,
            "input": user_message
        }):
            if chunk.content:
                full_response += chunk.content
                await queue.emit_token(chunk.content, step="generate")

        return full_response

    # ================================================================
    # 智能检索
    # ================================================================

    async def _smart_retrieve(
        self,
        query: str,
        strategy: str,
        queue: EventQueue,
        needs: dict | None = None,
    ) -> dict:
        """智能检索 - 优先 NL2SQL/RAG，失败时用直接数据库查询兜底，并显式返回商品/评价上下文"""
        product_parts: list[str] = []
        review_parts: list[str] = []
        diagnostics: list[str] = []
        needs = needs or {'needs_review': strategy in ('rag_only', 'mixed'), 'needs_product': strategy in ('sql_only', 'mixed')}

        hybrid_ok = False
        try:
            from app.retrieval.fusion import HybridRetriever
            retriever = HybridRetriever()

            if strategy in ("sql_only", "mixed", "rag_only"):
                await queue.emit_tool_call("NL2SQL+RAG", query[:100], step="retrieval")
                result = retriever.search(query)
                products = result.get("products", [])
                rag_results = result.get("rag_results", [])

                if products:
                    product_parts.append("【商品数据（NL2SQL）】")
                    for p in products[:5]:
                        if isinstance(p, dict) and "name" in p:
                            reviews = " ".join(p.get("related_reviews", [])[:2])
                            product_parts.append(
                                f"- {p['name']} ¥{p.get('price', 'N/A')} {p.get('brand', '')} {p.get('description', '')[:60]}{' 评价:' + reviews[:60] if reviews else ''}"
                            )

                if rag_results:
                    review_parts.append("【用户评价/攻略（RAG）】")
                    for r in rag_results[:5]:
                        if isinstance(r, dict) and "content" in r:
                            review_parts.append(f"- {r['content'][:200]}")

                if product_parts or review_parts:
                    await queue.emit_tool_result(
                        "NL2SQL+RAG", f"商品:{len(products)} 评价:{len(rag_results)}", step="retrieval"
                    )
                    hybrid_ok = True
                else:
                    diagnostics.append("高级检索返回空结果，准备进入降级流程。")

        except Exception as e:
            print(f"[RAG-ERROR] HybridRetriever 失败: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            diagnostics.append(f"高级检索不可用({type(e).__name__}): {str(e)[:220]}")
            await queue.emit(EventType.RETRIEVAL, {
                "status": "fallback",
                "message": f"高级检索不可用({type(e).__name__})，进入降级检索",
                "detail": str(e)[:300],
                "strategy": strategy
            }, step="retrieval")

        # 商品兜底
        if needs['needs_product'] and not product_parts:
            await queue.emit_tool_call("SQL直查", query[:100], step="retrieval")
            try:
                db_products = self._direct_db_search(query)
                if db_products:
                    product_parts.append("【商品数据（数据库直查）】")
                    for p in db_products[:8]:
                        product_parts.append(
                            f"- {p['name']} | ¥{p['price']} | {p['brand']} | {p['category']} | ⭐{p['rating']} | {'防水' if p.get('waterproof') else ''} | {p['description'][:50]}"
                        )
                    await queue.emit_tool_result("SQL直查", f"找到 {len(db_products)} 件商品", step="retrieval")
                else:
                    all_products = self._direct_db_search("")
                    if all_products:
                        product_parts.append("【全部商品（供参考）】")
                        for p in all_products[:10]:
                            product_parts.append(f"- {p['name']} | ¥{p['price']} | {p['brand']} | {p['category']}")
                        diagnostics.append("未精确命中商品关键词，已返回全部商品作为参考。")
                        await queue.emit_tool_result("SQL直查", f"返回 {len(all_products)} 件商品供参考", step="retrieval")
            except Exception as e:
                diagnostics.append(f"商品直查失败({type(e).__name__}): {str(e)[:160]}")
                await queue.emit_tool_result("SQL直查", f"数据库查询失败: {str(e)[:80]}", step="retrieval")

        # 评价兜底：只要用户真实需求包含评价，就必须尝试补一条评价路径
        if needs['needs_review'] and not review_parts:
            diagnostics.append("评价主链路未返回结果，启动评价降级检索。")
            try:
                db_reviews = self._direct_review_search(query)
                if db_reviews:
                    review_parts.append("【用户评价（数据库降级）】")
                    for r in db_reviews[:5]:
                        product_name = r.get('product_name') or f"商品#{r.get('product_id', '?')}"
                        content = (r.get('content') or '')[:160]
                        review_parts.append(f"- {product_name} ⭐{r.get('rating', '?')}: {content}")
                    await queue.emit_tool_result("评价降级(SQL)", f"命中 {len(db_reviews)} 条评价", step="retrieval")
                else:
                    text_reviews = self._fallback_text_review_search(query)
                    if text_reviews:
                        review_parts.append("【用户评价（本地文本降级）】")
                        review_parts.extend([f"- {line[:200]}" for line in text_reviews[:5]])
                        diagnostics.append("数据库评价未命中关键词，已回退到本地评价文本检索。")
                        await queue.emit_tool_result("评价降级(Text)", f"命中 {len(text_reviews)} 条本地评价片段", step="retrieval")
                    else:
                        diagnostics.append("评价降级检索仍未命中，最终回答必须明确提示评价信息缺失。")
            except Exception as e:
                diagnostics.append(f"评价降级检索失败({type(e).__name__}): {str(e)[:160]}")

        if diagnostics:
            await queue.emit(EventType.RETRIEVAL, {
                "status": "diagnostics",
                "strategy": strategy,
                "needs": needs,
                "items": diagnostics
            }, step="retrieval")

        return {
            'product_context': '\n'.join(product_parts).strip(),
            'review_context': '\n'.join(review_parts).strip(),
            'diagnostics': diagnostics,
            'strategy': strategy,
            'needs': needs
        }




    def _direct_db_search(self, query: str) -> list:
        """直接数据库查询 - 不依赖任何 LLM，纯 SQL"""
        import sqlite3
        import re

        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "products.db"
        )

        if not os.path.exists(db_path):
            return []

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        conditions = []
        params = []

        # 提取价格条件
        price_match = re.search(r'(\d+)\s*[元块]', query)
        if price_match:
            max_price = int(price_match.group(1))
            if "以内" in query or "以下" in query or "不超过" in query:
                conditions.append("price <= ?")
                params.append(max_price)
            elif "以上" in query:
                conditions.append("price >= ?")
                params.append(max_price)
            else:
                # "500左右" → ±30%
                conditions.append("price BETWEEN ? AND ?")
                params.extend([int(max_price * 0.7), int(max_price * 1.3)])

        # 提取分类关键词（注意：鞋在"服装"分类下）
        category_map = {
            "衣服": "服装", "服装": "服装", "T恤": "服装", "衬衫": "服装",
            "裤子": "服装", "外套": "服装", "连衣裙": "服装",
            "户外": "户外", "露营": "户外", "帐篷": "户外",
            "登山": "户外", "冲锋衣": "户外", "睡袋": "户外", "背包": "户外",
            "手机": "电子", "电脑": "电子", "电子": "电子",
            "泳": "户外", "浮潜": "户外", "防晒": "户外",
        }
        for keyword, cat in category_map.items():
            if keyword in query:
                conditions.append("category = ?")
                params.append(cat)
                break

        # 提取商品名关键词
        name_keywords = []
        product_words = ["跑", "鞋", "T恤", "衬衫", "牛仔", "帐篷", "睡袋", "手机", "电脑",
                        "冲锋衣", "登山鞋", "泳衣", "防晒", "背包", "登山杖", "连衣裙",
                        "外套", "裤子", "瑜伽", "浮潜"]
        for w in product_words:
            if w in query:
                name_keywords.append(w)

        if name_keywords:
            name_conds = " OR ".join(["(name LIKE ? OR description LIKE ?)" for _ in name_keywords])
            conditions.append(f"({name_conds})")
            for kw in name_keywords:
                params.extend([f"%{kw}%", f"%{kw}%"])

        # 提取品牌（用 OR 匹配多个品牌）
        brands = ["Nike", "Adidas", "小米", "华为", "iPhone", "苹果", "优衣库", "ZARA",
                  "波司登", "Salomon", "迪卡侬", "Levi\'s", "海澜之家", "安踏", "李宁",
                  "Apple", "Asics", "Lululemon", "牧高笛", "始祖鸟"]
        matched_brands = [b for b in brands if b.lower() in query.lower()]
        if matched_brands:
            # 匹配品牌名 或 商品名中包含品牌关键词
            brand_conds = " OR ".join(["(brand LIKE ? OR name LIKE ?)" for _ in matched_brands])
            conditions.append(f"({brand_conds})")
            for b in matched_brands:
                params.extend([f"%{b}%", f"%{b}%"])

        # 构建 SQL
        if conditions:
            sql = "SELECT * FROM products WHERE " + " AND ".join(conditions) + " ORDER BY rating DESC LIMIT 10"
        else:
            sql = "SELECT * FROM products ORDER BY rating DESC LIMIT 10"

        try:
            rows = conn.execute(sql, params).fetchall()
            results = [dict(row) for row in rows]
        except Exception:
            # SQL 失败时返回全部商品
            rows = conn.execute("SELECT * FROM products LIMIT 10").fetchall()
            results = [dict(row) for row in rows]
        finally:
            conn.close()

        return results

    # ================================================================
    # 记忆管理
    # ================================================================

    def _save_memories(self, user_id: str, session_id: str, intent: str, user_message: str):
        """保存记忆"""
        history = self.short_term_memory.get_history(session_id)
        if len(history) % 20 == 0 and len(history) > 0 and self.compressor:
            summary = self.compressor.compress(history)
            if self.long_term_memory:
                self.long_term_memory.save_summary(user_id, summary)

        if intent in ("shopping", "travel") and self.long_term_memory:
            self.long_term_memory.save_event(
                user_id,
                event_type=intent,
                event_data={"action": "对话", "object": user_message[:50], "scene": intent}
            )

    # ================================================================
    # 兼容旧接口（同步）
    # ================================================================

    async def process(
        self,
        user_message: str,
        user_id: str = "user_001",
        session_id: str = "default"
    ) -> Dict[str, Any]:
        """兼容旧接口 - 收集所有事件返回最终结果"""
        self.short_term_memory.add_message(session_id, "user", user_message)
        classification = await self.classifier.aclassify(user_message)
        chat_history = self.short_term_memory.get_langchain_messages(session_id, last_n=10)

        if classification.intent in ("shopping", "customer_service"):
            response = self.shopping_agent.process(user_message, user_id, chat_history)
            response_text = response.get("response", "")
        elif classification.intent == "travel":
            if classification.route == "plan_and_execute":
                response = self.travel_agent.plan_trip(user_message, user_id)
                response_text = response.get("plan", "")
            else:
                response = self.travel_agent.chat(user_message, user_id)
                response_text = response.get("response", "")
        elif classification.intent == "negotiation":
            response_text = "请在「社交协商」标签页中配置参与者信息后开始协商。"
        else:
            general_prompt = ChatPromptTemplate.from_messages([
                ("system", "你是 SmartLife Agent 智能助手。用中文回复。"),
                *[(msg["role"], msg["content"]) for msg in self.short_term_memory.get_history(session_id, last_n=5)],
                ("user", user_message)
            ])
            chain = general_prompt | self.llm
            result = chain.invoke({})
            response_text = result.content

        self.short_term_memory.add_message(session_id, "assistant", response_text)
        self._save_memories(user_id, session_id, classification.intent, user_message)

        return {
            "response": response_text,
            "classification": classification.model_dump(),
            "agent_used": classification.intent,
            "memories": {
                "short_term_count": len(self.short_term_memory.get_history(session_id)),
                "cross_scene_memories": len(
                    self.long_term_memory.get_cross_scene_memories(user_id, classification.intent)
                ) if self.long_term_memory else 0
            }
        }


# === 全局实例 ===
_orchestrator_v2 = None

def get_orchestrator_v2(model_name: str = None) -> OrchestratorV2:
    global _orchestrator_v2
    if _orchestrator_v2 is None:
        _orchestrator_v2 = OrchestratorV2(model_name)
    return _orchestrator_v2

async def process_user_message_streaming(
    message: str, user_id: str = "user_001", session_id: str = "default"
) -> AsyncGenerator[str, None]:
    orchestrator = get_orchestrator_v2()
    async for sse in orchestrator.process_streaming(message, user_id, session_id):
        yield sse

async def process_user_message(
    message: str, user_id: str = "user_001", session_id: str = "default"
) -> Dict[str, Any]:
    orchestrator = get_orchestrator_v2()
    return await orchestrator.process(message, user_id, session_id)
