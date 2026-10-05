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
import uuid
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
from app.agents.react_graph import ToolReActGraph
from app.agents.shopping_graph import ShoppingGraph
from app.negotiation.graph import NegotiationGraph
from app.memory.short_term import ShortTermMemory
from app.memory.long_term import LongTermMemory
from app.memory.compressor import MemoryCompressor
from app.memory.md_memory import MDMemory
from app.memory.extractor import MemoryExtractor, MemoryExtractionResult
from app.memory.approval_graph import MemoryApprovalGraph
from app.memory.repository import MemoryRepository
from app.session import build_thread_id
from app.tools import get_safe_tools
from app.observability import log_exception


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
        self._rag_retriever = None
        self._nl2sql_chain = None
        self._retrieval_service = None
        self._reranker = None
        self._memory_approval_graph = None
        self._react_graph = None
        self._shopping_graph = None
        self._memory_repository = None
        self._last_classifications = {}

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

    @property
    def rag_retriever(self):
        if getattr(self, "_rag_retriever", None) is None:
            from app.retrieval.rag import RAGRetriever
            self._rag_retriever = RAGRetriever()
        return self._rag_retriever

    @property
    def nl2sql_chain(self):
        if getattr(self, "_nl2sql_chain", None) is None:
            from app.retrieval.nl2sql import NL2SQLChain
            self._nl2sql_chain = NL2SQLChain()
        return self._nl2sql_chain

    @property
    def retrieval_service(self):
        if getattr(self, "_retrieval_service", None) is None:
            from app.retrieval.service import RetrievalService
            self._retrieval_service = RetrievalService(
                nl2sql_chain=self.nl2sql_chain,
                rag_retriever=self.rag_retriever,
                reranker=getattr(self, "_reranker", None),
            )
        return self._retrieval_service

    @property
    def memory_approval_graph(self):
        if getattr(self, "_memory_approval_graph", None) is None:
            self._memory_approval_graph = MemoryApprovalGraph(
                persist=self.save_user_selected_memories
            )
        return self._memory_approval_graph

    @property
    def memory_repository(self):
        if getattr(self, "_memory_repository", None) is None:
            self._memory_repository = MemoryRepository(
                self.md_memory,
                self.long_term_memory,
            )
        return self._memory_repository

    @property
    def react_graph(self):
        if getattr(self, "_react_graph", None) is None:
            self._react_graph = ToolReActGraph(
                model=self.llm,
                tools=get_safe_tools(),
                max_steps=6,
                tool_timeout=10.0,
            )
        return self._react_graph

    @property
    def shopping_graph(self):
        if getattr(self, "_shopping_graph", None) is None:
            self._shopping_graph = ShoppingGraph(self)
        return self._shopping_graph

    # ================================================================
    # Streaming 入口
    # ================================================================

    async def process_streaming(
        self,
        user_message: str,
        user_id: str,
        session_id: str,
        scene: str = "general"
    ) -> AsyncGenerator[str, None]:
        """
        全链路流式处理 - 返回 SSE 事件流

        Yields:
            SSE 格式的事件字符串
        """
        thread_id = build_thread_id(user_id, scene, session_id)
        queue = EventQueue()

        async def _process():
            try:
                await self._do_process(user_message, user_id, thread_id, queue)
            except Exception as e:
                log_exception("orchestrator.streaming", e, {"thread_id": thread_id})
                await queue.emit_error(f"处理失败: {str(e)}")
            finally:
                await queue.finish()

        task = asyncio.create_task(_process())
        try:
            async for sse in queue.to_sse():
                yield sse
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

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
                log_exception("orchestrator.negotiation", e, {"scenario": scenario})
                await queue.emit_error(f"协商失败: {str(e)}")
            finally:
                await queue.finish()

        task = asyncio.create_task(_process())
        try:
            async for sse in queue.to_sse():
                yield sse
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ================================================================
    # 核心处理
    # ================================================================

    async def begin_turn(self, user_message: str, thread_id: str) -> None:
        """记录用户消息，作为一次图执行的起点。"""
        self.short_term_memory.add_message(thread_id, "user", user_message)

    async def classify_turn(
        self,
        user_message: str,
        thread_id: str,
        queue: EventQueue,
    ) -> ClassificationResult:
        """执行统一分类和产品关键词安全网。"""
        await queue.emit(EventType.STEP, {
            "step": 1, "total": 4,
            "name": "统一分类",
            "description": "小模型分析任务类型和路由策略"
        }, step="classify")

        history_for_classification = self.short_term_memory.get_history(thread_id, last_n=11)[:-1]
        previous_classification = self._last_classifications.get(thread_id)
        classification = await self.classifier.aclassify_streaming(
            user_message,
            queue,
            history=history_for_classification,
            previous=previous_classification,
        )
        self._last_classifications[thread_id] = classification

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
                self._last_classifications[thread_id] = classification

        return classification

    def get_chat_history(self, thread_id: str) -> List[Any]:
        return self.short_term_memory.get_langchain_messages(thread_id, last_n=10)

    async def run_shopping_turn(
        self,
        user_message: str,
        user_id: str,
        thread_id: str,
        classification: ClassificationResult,
        queue: EventQueue,
    ) -> str:
        return await self.shopping_graph.run_streaming(
            user_message,
            user_id,
            thread_id,
            classification.model_dump(),
            queue,
        )

    async def run_travel_turn(
        self,
        user_message: str,
        user_id: str,
        thread_id: str,
        classification: ClassificationResult,
        queue: EventQueue,
    ) -> str:
        chat_history = self.get_chat_history(thread_id)
        if classification.route == "plan_and_execute":
            return await self.travel_agent.plan_trip_streaming(
                user_message, user_id, queue, chat_history=chat_history
            )
        return await self.travel_agent.chat_streaming(user_message, user_id, queue)

    async def run_negotiation_turn(
        self,
        _user_message: str,
        _user_id: str,
        _thread_id: str,
        _classification: ClassificationResult,
        queue: EventQueue,
    ) -> str:
        response_text = "请在「社交协商」标签页中配置参与者信息后开始协商。"
        await queue.emit_token(response_text, step="agent")
        return response_text

    async def run_react_turn(
        self,
        user_message: str,
        _user_id: str,
        thread_id: str,
        _classification: ClassificationResult,
        queue: EventQueue,
    ) -> str:
        return await self._run_react_streaming(
            user_message,
            self.get_chat_history(thread_id),
            queue,
            thread_id,
        )

    async def run_general_turn(
        self,
        user_message: str,
        _user_id: str,
        thread_id: str,
        _classification: ClassificationResult,
        queue: EventQueue,
    ) -> str:
        return await self._run_general_streaming(
            user_message,
            thread_id,
            self.get_chat_history(thread_id),
            queue,
        )

    async def finish_turn(
        self,
        thread_id: str,
        response_text: str,
        classification: ClassificationResult,
        queue: EventQueue,
    ) -> None:
        """保存助手回复并发送统一完成事件。"""
        self.short_term_memory.add_message(thread_id, "assistant", response_text)
        await queue.emit(EventType.DONE, {
            "response": response_text,
            "classification": classification.model_dump(),
            "agent_used": classification.intent,
            "memories": {
                "short_term_count": len(self.short_term_memory.get_history(thread_id)),
            }
        }, step="done")

    async def _do_process(
        self,
        user_message: str,
        user_id: str,
        thread_id: str,
        queue: EventQueue
    ):
        """兼容入口；新的顶层控制流由 LangGraphOrchestrator 承担。"""
        await self.begin_turn(user_message, thread_id)
        classification = await self.classify_turn(user_message, thread_id, queue)

        if classification.intent in ("shopping", "customer_service"):
            response_text = await self.run_shopping_turn(
                user_message, user_id, thread_id, classification, queue
            )
        elif classification.intent == "travel":
            response_text = await self.run_travel_turn(
                user_message, user_id, thread_id, classification, queue
            )
        elif classification.intent == "negotiation":
            response_text = await self.run_negotiation_turn(
                user_message, user_id, thread_id, classification, queue
            )
        elif classification.intent == "general" and classification.route == "react":
            response_text = await self.run_react_turn(
                user_message, user_id, thread_id, classification, queue
            )
        else:
            response_text = await self.run_general_turn(
                user_message, user_id, thread_id, classification, queue
            )

        await self.finish_turn(thread_id, response_text, classification, queue)

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

        enriched_participants = self.negotiation_agent.enrich_participants(participants, scenario)
        result = await self.negotiation_graph.run_streaming(
            enriched_participants,
            scenario,
            queue,
            thread_id=f"negotiation:{scenario}:{','.join(p.get('user_id', 'unknown') for p in participants)}",
        )

        await queue.emit(EventType.DONE, {
            "negotiation_result": result,
            "scenario": scenario,
            "response": result.get("plan_text", ""),
        }, step="done")

    # ================================================================
    # 购物流式处理
    # ================================================================

    async def plan_shopping_retrieval(
        self,
        user_message: str,
        classification: ClassificationResult,
        queue: EventQueue,
    ):
        """购物子图节点：确定 SQL/RAG 需求和检索策略。"""
        needs = self._classify_data_needs(user_message, classification)
        planned_strategy = needs["strategy"]
        await queue.emit(EventType.STEP, {
            "step": 2, "total": 4,
            "name": "数据检索",
            "description": (f"检索策略: {planned_strategy} | 需要商品: {needs['needs_product']} "
                            f"| 需要评价: {needs['needs_review']}")
        }, step="retrieval")
        return needs, planned_strategy

    async def retrieve_shopping_context(
        self,
        user_message: str,
        planned_strategy: str,
        needs: Dict[str, Any],
        queue: EventQueue,
    ) -> Dict[str, Any]:
        """购物子图节点：执行 SQL/RAG 检索。"""
        return await self._smart_retrieve(
            user_message,
            planned_strategy,
            queue,
            needs=needs,
        )

    async def generate_shopping_response(
        self,
        user_message: str,
        user_id: str,
        thread_id: str,
        classification: ClassificationResult,
        retrieval: Dict[str, Any],
        queue: EventQueue,
    ) -> str:
        """购物子图节点：构建上下文并流式生成回答。"""
        chat_history = self.get_chat_history(thread_id)
        cross_memories = []
        if self.long_term_memory:
            cross_memories = self.long_term_memory.get_cross_scene_memories(
                user_id,
                "shopping",
                query=user_message,
            )
        memory_context = ""
        if cross_memories:
            memory_context = "\n用户历史偏好：\n" + "\n".join(
                [m["content"] for m in cross_memories[:3]]
            )

        full_input = user_message
        if retrieval.get("product_context"):
            full_input += f"\n\n【商品信息】\n{retrieval['product_context']}"
        if retrieval.get("review_context"):
            full_input += f"\n\n【评价/口碑信息】\n{retrieval['review_context']}"
        if retrieval.get("diagnostics"):
            diag_text = "\n".join([f"- {d}" for d in retrieval["diagnostics"]])
            full_input += f"\n\n【检索诊断信息】\n{diag_text}"
        if memory_context:
            full_input += f"\n\n【用户历史偏好】\n{memory_context}"

        await queue.emit(EventType.STEP, {
            "step": 3, "total": 4,
            "name": "生成回复",
            "description": "大模型流式生成购物建议"
        }, step="generate")
        await queue.emit_thinking("🧠 正在生成购物建议...", step="generate")

        prompt = ChatPromptTemplate.from_messages([
            ('system', '''你是一个智能导购助手，帮用户找到最合适的商品。

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
- 结束时说明"以上商品信息来自平台数据库"'''),
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

    @staticmethod
    def validate_shopping_response(response: str, retrieval: Dict[str, Any]) -> None:
        """购物子图节点：执行最低限度的输出契约校验。"""
        if not response or not response.strip():
            raise ValueError("购物回答为空")
        if not isinstance(retrieval, dict):
            raise ValueError("检索结果格式错误")

    async def _run_shopping_streaming(
        self,
        user_message: str,
        user_id: str,
        chat_history: List,
        classification: ClassificationResult,
        queue: EventQueue,
        thread_id: Optional[str] = None,
    ) -> str:
        """兼容入口；实际购物控制流由 ShoppingGraph 承担。"""
        return await self.shopping_graph.run_streaming(
            user_message,
            user_id,
            thread_id or build_thread_id(user_id, "shopping", "legacy"),
            classification.model_dump(),
            queue,
        )

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

    async def _run_react_streaming(
        self,
        user_message: str,
        chat_history: List,
        queue: EventQueue,
        thread_id: str,
    ) -> str:
        """通过真实工具调用循环处理 general/react 请求。"""
        await queue.emit(EventType.STEP, {
            "step": 2,
            "total": 4,
            "name": "工具推理",
            "description": "模型决定是否调用天气、路线或时间工具",
        }, step="react")

        async def on_event(event: str, data: Dict[str, Any]):
            if event == "tool_call":
                await queue.emit_tool_call(
                    data.get("tool", ""),
                    data.get("args", {}),
                    step="react",
                )
            elif event == "tool_result":
                if data.get("ok"):
                    await queue.emit_tool_result(
                        data.get("tool", ""),
                        str(data.get("result", "")),
                        step="react",
                    )
                else:
                    await queue.emit_error(
                        f"工具 {data.get('tool', '')} 执行失败: {data.get('error', '')}",
                        step="react",
                    )
            elif event == "finalized":
                await queue.emit_thinking("🧠 工具推理完成", step="react")

        self.react_graph.on_event = on_event
        messages = list(chat_history) + [HumanMessage(content=user_message)]
        result = await self.react_graph.run_streaming(messages, thread_id=thread_id)
        response = result.get("response", "") or "工具推理已完成，但没有生成最终回答。"
        await queue.emit_token(response, step="react")
        return response

    async def _smart_retrieve(
        self,
        query: str,
        strategy: str,
        queue: EventQueue,
        needs: dict = None
    ) -> dict:
        """智能检索 - 统一委托 RetrievalService，编排层只处理事件和上下文。"""
        retrieval = self.retrieval_service.retrieve(
            query,
            strategy=strategy,
            top_k=5,
            needs=needs,
        )

        if strategy in ("sql_only", "mixed"):
            await queue.emit_tool_call("nl2sql", retrieval.get("sql", ""), step="retrieval")
        if strategy in ("rag_only", "mixed"):
            await queue.emit_tool_call(
                "rag",
                f"返回 {len(retrieval.get('documents', []))} 条文档",
                step="retrieval",
            )

        product_parts = []
        if retrieval.get("products"):
            product_parts.append("商品信息：")
            for product in retrieval["products"][:5]:
                product_parts.append(
                    f"- {product.get('name', '')} | ¥{product.get('price', '')} | "
                    f"{product.get('brand', '')} | {str(product.get('description', ''))[:50]}"
                )

        review_parts = []
        if retrieval.get("documents"):
            review_parts.append("评价/口碑信息：")
            for document in retrieval["documents"][:3]:
                review_parts.append(f"- {document['content'][:100]}")

        return {
            'product_context': '\n'.join(product_parts).strip(),
            'review_context': '\n'.join(review_parts).strip(),
            'diagnostics': retrieval.get("diagnostics", []),
            'rag_ok': retrieval.get("rag_ok"),
            'rag_error': retrieval.get("rag_error"),
            'strategy': retrieval.get("strategy", strategy),
            'needs': retrieval.get("needs", needs)
        }

    # ================================================================
    # 记忆管理（新版本 - 手动保存）
    # ================================================================

    def _existing_memory_contents(self, user_id: str) -> List[str]:
        if not self.md_memory:
            return []
        return [
            memory.get("content", "")
            for memory in self.md_memory.get_all_memories(user_id)
            if memory.get("content")
        ]

    @staticmethod
    def _latest_user_message(history: List[Any]) -> List[Dict[str, str]]:
        for message in reversed(history or []):
            role = message.get("role") if isinstance(message, dict) else getattr(message, "type", "")
            content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
            if role == "user" and str(content).strip():
                return [{"role": "user", "content": str(content)}]
        return []

    def extract_candidate_memories(
        self,
        user_id: str,
        session_id: str,
        scene: str,
        excluded_contents: Optional[List[str]] = None,
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
        
        thread_id = build_thread_id(user_id, scene, session_id)
        history = self.short_term_memory.get_history(thread_id)
        current_user_message = self._latest_user_message(history)
        if not current_user_message:
            return []
        
        # 提取候选记忆
        candidates = self.extractor.extract_candidates(
            current_user_message,
            existing_contents=self._existing_memory_contents(user_id),
            excluded_contents=excluded_contents,
        )
        return candidates

    def extract_candidate_memories_result(
        self,
        user_id: str,
        session_id: str,
        scene: str,
        excluded_contents: Optional[List[str]] = None,
    ) -> MemoryExtractionResult:
        """提取候选记忆，并区分正常空结果与执行失败。"""
        if not self.extractor:
            return MemoryExtractionResult(
                diagnostic="记忆提取失败：记忆提取器未初始化",
                status="error",
            )
        thread_id = build_thread_id(user_id, scene, session_id)
        history = self.short_term_memory.get_history(thread_id)
        return self.extractor.extract_candidates_result(
            self._latest_user_message(history),
            existing_contents=self._existing_memory_contents(user_id),
            excluded_contents=excluded_contents,
        )

    def extract_candidate_memories_with_diag(
        self,
        user_id: str,
        session_id: str,
        scene: str,
        excluded_contents: Optional[List[str]] = None,
    ):
        """兼容旧接口，返回 ``(candidates, diagnostic)``。"""
        result = self.extract_candidate_memories_result(
            user_id,
            session_id,
            scene,
            excluded_contents=excluded_contents,
        )
        return result.candidates, result.diagnostic

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
                if category in ("shopping", "travel"):
                    memory_id = self.memory_repository.save_event(user_id, content, category)
                else:
                    memory_id = self.memory_repository.save_preference(user_id, content, category)
                saved_ids.append(memory_id)
        
        return saved_ids

    def start_memory_approval(
        self,
        user_id: str,
        session_id: str,
        scene: str,
        candidates: List[Dict[str, Any]],
        approval_id: str = None,
    ) -> Dict[str, Any]:
        """启动候选记忆审批，并在 LangGraph interrupt 处暂停。"""
        approval_id = approval_id or uuid.uuid4().hex
        thread_id = f"{build_thread_id(user_id, scene, session_id)}:memory:{approval_id}"
        result = self.memory_approval_graph.start(approval_id, user_id, candidates)
        result["thread_id"] = thread_id
        return result

    def resume_memory_approval(
        self,
        approval_id: str,
        decision: Dict[str, Any]
    ) -> Dict[str, Any]:
        """恢复候选记忆审批 checkpoint 并执行最终决定。"""
        return self.memory_approval_graph.resume(approval_id, decision)

    def compress_conversation(
        self,
        user_id: str,
        session_id: str,
        scene: str
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
        
        thread_id = build_thread_id(user_id, scene, session_id)
        history = self.short_term_memory.get_history(thread_id)
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
        
        return self.memory_repository.save_summary(user_id, summary, {
            "category": "compressed_summary",
            "source": "manual_compress",
        })

    # ================================================================
    # 兼容旧接口（同步）
    # ================================================================

    async def process(
        self,
        user_message: str,
        user_id: str,
        session_id: str,
        scene: str = "general"
    ) -> Dict[str, Any]:
        """兼容旧接口 - 收集所有事件返回最终结果"""
        thread_id = build_thread_id(user_id, scene, session_id)
        self.short_term_memory.add_message(thread_id, "user", user_message)
        history_for_classification = self.short_term_memory.get_history(thread_id, last_n=11)[:-1]
        previous_classification = self._last_classifications.get(thread_id)
        classification = await self.classifier.aclassify(
            user_message,
            history=history_for_classification,
            previous=previous_classification,
        )
        self._last_classifications[thread_id] = classification
        chat_history = self.short_term_memory.get_langchain_messages(thread_id, last_n=10)

        if classification.intent in ("shopping", "customer_service"):
            response = self.shopping_agent.process(user_message, user_id, chat_history)
            response_text = response.get("response", "")
        elif classification.intent == "travel":
            if classification.route == "plan_and_execute":
                response = self.travel_agent.plan_trip(user_message, user_id, chat_history=chat_history)
                response_text = response.get("plan", "")
            else:
                response = self.travel_agent.chat(user_message, user_id)
                response_text = response.get("response", "")
        elif classification.intent == "negotiation":
            response_text = "请在「社交协商」标签页中配置参与者信息后开始协商。"
        else:
            general_prompt = ChatPromptTemplate.from_messages([
                ("system", "你是 SmartLife Agent 智能助手。用中文回复。"),
                *[(msg["role"], msg["content"]) for msg in self.short_term_memory.get_history(thread_id, last_n=5)],
                ("user", user_message)
            ])
            chain = general_prompt | self.llm
            result = chain.invoke({})
            response_text = result.content

        self.short_term_memory.add_message(thread_id, "assistant", response_text)

        return {
            "response": response_text,
            "classification": classification.model_dump(),
            "agent_used": classification.intent,
            "memories": {
                "short_term_count": len(self.short_term_memory.get_history(thread_id)),
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
    message: str, user_id: str, session_id: str, scene: str = "general"
) -> AsyncGenerator[str, None]:
    orchestrator = get_orchestrator_v2()
    async for sse in orchestrator.process_streaming(message, user_id, session_id, scene):
        yield sse

async def process_user_message(
    message: str, user_id: str, session_id: str, scene: str = "general"
) -> Dict[str, Any]:
    orchestrator = get_orchestrator_v2()
    return await orchestrator.process(message, user_id, session_id, scene)
