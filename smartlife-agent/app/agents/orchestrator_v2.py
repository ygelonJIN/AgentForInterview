"""
Orchestrator v2 - 全链路 Streaming 主协调器

核心改动：
1. 统一分类器替代 Router + Intent 双调用
2. 所有 LLM 调用使用 astream() 替代 invoke()
3. 全程通过 EventQueue 推送实时事件
4. 检索策略按分类结果智能路由
5. 复用子 Agent 的 streaming 方法，不内联重复逻辑

记忆系统改动：
- 去掉自动压缩（每20条消息）
- 去掉自动保存事件
- 用户手动触发保存
- 对话结束后提取候选记忆，用户决定是否保留
- 向量库 + MD 文档并存
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
from app.memory.md_memory import MDMemory
from app.memory.extractor import MemoryExtractor


class OrchestratorV2:
    """
    主协调器 v2 - 全链路流式处理

    流程：
    1. 统一分类（小模型，一次调用）→ 路由 + 意图 + 检索策略
    2. 根据意图路由到子 Agent
    3. 子 Agent 流式输出（astream）
    4. 全程事件推送
    5. 对话结束后提取候选记忆，用户决定是否保留
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
        self._md_memory = None
        self._extractor = None

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

    @property
    def md_memory(self):
        if self._md_memory is None:
            try:
                self._md_memory = MDMemory()
            except Exception:
                pass
        return self._md_memory

    @property
    def extractor(self):
        if self._extractor is None:
            try:
                self._extractor = MemoryExtractor()
            except Exception:
                pass
        return self._extractor

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
    # 核心处理
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
                    "confidence": classification.confidence,
                    "reason": "安全网：检测到产品关键词",
                    "method": "keyword"
                }, step="classify")

        # Step 4: 路由到子 Agent
        response_text = ""
        response_data = {}

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

        # Step 5: 保存回复到短期记忆
        self.short_term_memory.add_message(session_id, "assistant", response_text)

        # Step 6: 发送完成事件
        await queue.emit(EventType.DONE, {
            "response": response_text,
            "classification": classification.model_dump(),
            "agent_used": classification.intent,
            "memories": {
                "short_term_count": len(self.short_term_memory.get_history(session_id)),
            }
        }, step="done")

    async def _do_negotiation(
        self,
        participants: List[Dict[str, Any]],
        scenario: str,
        queue: EventQueue
    ):
        """协商处理逻辑"""
        await queue.emit(EventType.STEP, {
            "step": 1, "total": 2,
            "name": "协商分析",
            "description": "分析参与者偏好和冲突"
        }, step="negotiation")

        result = await self.negotiation_graph.run_streaming(participants, scenario, queue)

        await queue.emit(EventType.DONE, {
            "negotiation_result": result,
            "scenario": scenario
        }, step="done")

    # ================================================================
    # 购物流式处理
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
            "description": (f"检索策略: {planned_strategy} | 需要商品: {needs['needs_product']} "
                            f"| 需要评价: {needs['needs_review']}")
        }, step="retrieval")

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
        await queue.emit(EventType.STEP, {
            "step": 3, "total": 4,
            "name": "生成回复",
            "description": "大模型流式生成购物建议"
        }, step="generate")

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

    # ================================================================
    # 检索策略
    # ================================================================

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
            'strategy': strategy
        }

    async def _emit_retrieval_plan(self, queue: 'EventQueue', strategy: str, needs: dict, source: str = 'policy'):
        """检索规划信息已并入 Step 2「数据检索」描述，避免重复推送 Step 2。"""
        return

    async def _run_general_streaming(
        self,
        user_message: str,
        session_id: str,
        chat_history: List,
        queue: EventQueue
    ) -> str:
        """通用对话 - 流式处理"""
        await queue.emit(EventType.STEP, {
            "step": 2, "total": 4,
            "name": "通用对话",
            "description": "处理通用对话请求"
        }, step="general")

        general_prompt = ChatPromptTemplate.from_messages([
            ("system", "你是 SmartLife Agent 智能助手，帮助用户购物和旅行规划。用中文回复。友好、专业、简洁。"),
            *[(msg.role, msg.content) for msg in chat_history[-5:]],
            ("user", user_message)
        ])

        chain = general_prompt | self.llm
        full_response = ""

        async for chunk in chain.astream({}):
            if chunk.content:
                full_response += chunk.content
                await queue.emit_token(chunk.content, step="general")

        return full_response

    async def _smart_retrieve(
        self,
        query: str,
        strategy: str,
        queue: EventQueue,
        needs: dict = None
    ) -> dict:
        """智能检索 - 根据策略选择检索方式"""
        from app.retrieval.rag import RAGRetriever
        from app.retrieval.nl2sql import NL2SQLChain
        from app.retrieval.fusion import HybridRetriever

        product_parts = []
        review_parts = []
        diagnostics = []

        # SQL 检索
        if strategy in ("sql_only", "mixed"):
            try:
                nl2sql = NL2SQLChain()
                sql_result = nl2sql.query(query)
                
                if sql_result.get("error"):
                    diagnostics.append(f"NL2SQL 错误: {sql_result['error']}")
                    await queue.emit_tool_call("nl2sql", sql_result.get("sql", ""), step="retrieval")
                else:
                    await queue.emit_tool_call("nl2sql", sql_result.get("sql", ""), step="retrieval")
                    
                    if sql_result.get("results"):
                        product_parts.append("商品信息：")
                        for p in sql_result["results"][:5]:
                            product_parts.append(
                                f"- {p['name']} | ¥{p['price']} | {p['brand']} | {p['description'][:50]}"
                            )
                    else:
                        diagnostics.append("NL2SQL 未返回商品结果")
            except Exception as e:
                diagnostics.append(f"NL2SQL 异常: {str(e)}")

        # RAG 检索
        if strategy in ("rag_only", "mixed"):
            try:
                rag = RAGRetriever()
                rag_result = rag.retrieve(query, k=5)
                
                await queue.emit_tool_call("rag", f"返回 {len(rag_result.get('documents', []))} 条文档", step="retrieval")
                
                if rag_result.get("documents"):
                    review_parts.append("评价/口碑信息：")
                    for doc in rag_result["documents"][:3]:
                        review_parts.append(f"- {doc.page_content[:100]}")
                else:
                    diagnostics.append("RAG 未返回评价结果")
            except Exception as e:
                diagnostics.append(f"RAG 异常: {str(e)}")

        return {
            'product_context': '\n'.join(product_parts).strip(),
            'review_context': '\n'.join(review_parts).strip(),
            'diagnostics': diagnostics,
            'strategy': strategy,
            'needs': needs
        }

    # ================================================================
    # 记忆管理（新版本 - 手动保存）
    # ================================================================

    def extract_candidate_memories(
        self,
        user_id: str,
        session_id: str
    ) -> List[Dict[str, Any]]:
        """
        提取候选记忆（供用户确认）
        
        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            
        Returns:
            候选记忆列表
        """
        if not self.extractor:
            return []
        
        history = self.short_term_memory.get_history(session_id)
        if not history:
            return []
        
        # 提取候选记忆
        candidates = self.extractor.extract_candidates(history)
        return candidates

    def extract_candidate_memories_with_diag(self, user_id: str, session_id: str):
        """提取候选记忆 + 诊断信息，用于验证小模型是否真的执行"""
        if not self.extractor:
            return [], "记忆提取器未初始化"
        history = self.short_term_memory.get_history(session_id)
        return self.extractor.extract_candidates_with_diag(history)

    def get_candidate_display(self, candidates: List[Dict[str, Any]]) -> str:
        """
        获取候选记忆的显示文本
        
        Args:
            candidates: 候选记忆列表
            
        Returns:
            格式化的显示文本
        """
        if not self.extractor:
            return ""
        return self.extractor.format_for_display(candidates)

    def save_user_selected_memories(
        self,
        user_id: str,
        candidates: List[Dict[str, Any]],
        selected_indices: List[int]
    ) -> List[str]:
        """
        保存用户选择的记忆
        
        Args:
            user_id: 用户 ID
            candidates: 候选记忆列表
            selected_indices: 用户选择的索引列表
            
        Returns:
            保存的记忆 ID 列表
        """
        saved_ids = []
        
        for idx in selected_indices:
            if 0 <= idx < len(candidates):
                candidate = candidates[idx]
                content = candidate["content"]
                category = candidate.get("category", "general")
                
                # 保存到 MD 文档
                if self.md_memory:
                    if category in ("shopping", "travel"):
                        memory_id = self.md_memory.save_event(user_id, content, category)
                    else:
                        memory_id = self.md_memory.save_preference(user_id, content, category)
                    saved_ids.append(memory_id)
                
                # 保存到向量库
                if self.long_term_memory:
                    self.long_term_memory.save_summary(user_id, content, {
                        "category": category,
                        "source": "user_selected"
                    })
        
        return saved_ids

    def compress_conversation(
        self,
        user_id: str,
        session_id: str
    ) -> str:
        """
        手动压缩对话（用户触发）
        
        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            
        Returns:
            压缩后的摘要
        """
        if not self.compressor:
            return ""
        
        history = self.short_term_memory.get_history(session_id)
        if not history:
            return ""
        
        summary = self.compressor.compress(history)
        return summary

    def save_compressed_summary(
        self,
        user_id: str,
        summary: str
    ) -> str:
        """
        保存压缩后的摘要（用户确认后）
        
        Args:
            user_id: 用户 ID
            summary: 摘要内容
            
        Returns:
            保存的记忆 ID
        """
        if not self.md_memory:
            return ""
        
        memory_id = self.md_memory.save_preference(user_id, summary, "compressed_summary")
        
        if self.long_term_memory:
            self.long_term_memory.save_summary(user_id, summary, {
                "category": "compressed_summary",
                "source": "manual_compress"
            })
        
        return memory_id

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

        return {
            "response": response_text,
            "classification": classification.model_dump(),
            "agent_used": classification.intent,
            "memories": {
                "short_term_count": len(self.short_term_memory.get_history(session_id)),
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
