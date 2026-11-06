"""领域服务层。

这里只放**无编排**的能力：分类、检索、生成、校验、记忆读写、压缩。
控制流、路由、并行、重试、流式全部在 ``app/graphs`` 里由 LangGraph 承担。

旧 ``OrchestratorV2`` 的编排方法（process_streaming / run_*_turn / _do_process）
已删除；任何调用方都应改为驱动图。
"""
import asyncio
import os
import sys
import uuid
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from app.config import create_llm
from app.services.travel_service import TravelService
from app.classifier import ClassificationResult, get_classifier
from app.services.task_models import SharedConstraints
from app.memory.short_term import ShortTermMemory
from app.memory.conversation_store import ConversationStore
from app.memory.long_term import LongTermMemory
from app.memory.compressor import MemoryCompressor
from app.memory.md_memory import MDMemory
from app.memory.extractor import MemoryExtractor, MemoryExtractionResult
from app.memory.approval_graph import MemoryApprovalGraph
from app.memory.repository import MemoryRepository
from app.evaluation.ab_logging import ExperimentLogger
from app.retrieval.reranker import create_reranker
from app.retrieval.order_repository import OrderRepository
from app.session import build_thread_id
from app.tools import get_safe_tools
from app.graphs.events import EventSink
from app.streaming import EventType
from app.nlu import lexicon, signals
from app.trip_gear.intent import is_gear_request
from app.observability import extract_model_usage, log_exception, timed_span


class TurnServices:
    """一轮对话可用的领域能力集合（无状态编排）。

    图节点通过 ``runtime.context.services`` 拿到它；它自己不决定下一步做什么。
    """

    def __init__(self, model_name: str = None):
        self.model_name = model_name
        self.llm = create_llm(model_name)
        self.classifier = get_classifier()

        # 子服务
        self._travel_service = None
        self._order_repository = None
        self._trip_gear_service = None

        # 记忆
        self._short_term_memory = None
        self._conversation_store = None
        self._long_term_memory = None
        self._compressor = None
        self._md_memory = None
        self._extractor = None
        self._rag_retriever = None
        self._nl2sql_chain = None
        self._retrieval_service = None
        self._reranker = create_reranker()
        self._experiment_logger = (
            ExperimentLogger()
            if os.environ.get("SMARTLIFE_AB_LOGGING", "").strip().lower() in {"1", "true", "yes", "on"}
            else None
        )
        self._memory_approval_graph = None
        self._customer_service_graph = None
        self._general_plan_graph = None
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
    def conversation_store(self):
        if self._conversation_store is None:
            self._conversation_store = ConversationStore()
        return self._conversation_store

    @property
    def short_term_memory(self):
        if self._short_term_memory is None:
            self._short_term_memory = ShortTermMemory(
                conversation_store=self.conversation_store,
            )
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
    def travel_service(self) -> TravelService:
        """旅行上下文与规划能力的服务入口（不承担编排）。"""
        if self._travel_service is None:
            self._travel_service = TravelService(self.model_name)
        return self._travel_service

    @property
    def trip_gear_service(self):
        """行程约束派生 + 装备协同规划服务入口。"""
        if self._trip_gear_service is None:
            from app.trip_gear.service import TripGearService

            self._trip_gear_service = TripGearService(model_name=self.model_name)
        return self._trip_gear_service

    @property
    def order_repository(self):
        if self._order_repository is None:
            self._order_repository = OrderRepository()
        return self._order_repository

    def tools_for(self, actor_id: str, agent_id: str = "general"):
        """按 Actor 绑定的工具集；工具越权校验在工具注册表里完成。"""
        return get_safe_tools(actor_id=actor_id, agent_id=agent_id)

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

    async def begin_turn(self, user_message: str, thread_id: str) -> None:
        """记录用户消息；由 prepare 节点调用。"""
        self.short_term_memory.add_message(thread_id, "user", user_message)

    async def finish_turn(
        self,
        thread_id: str,
        response_text: str,
        classification: ClassificationResult,
        queue: EventSink,
    ) -> None:
        """保存助手回复并发送统一完成事件；由 finalize 节点调用。"""
        self.short_term_memory.add_message(thread_id, "assistant", response_text)
        payload = {
            "response": response_text,
            "classification": classification.model_dump(),
            "agent_used": getattr(classification, "agent_label", classification.intent),
            "memories": {
                "short_term_count": len(self.short_term_memory.get_history(thread_id)),
            },
        }
        if hasattr(queue, "emit_done"):
            await queue.emit_done(payload)
        else:
            await queue.emit(EventType.DONE, payload, step="done")

    async def classify(
        self,
        user_message: str,
        thread_id: str,
        queue: EventSink,
    ) -> ClassificationResult:
        """执行统一分类和产品关键词安全网。"""
        await queue.emit(EventType.STEP, {
            "step": 1, "total": 4,
            "name": "统一分类",
            "description": "小模型分析任务类型和路由策略"
        }, step="classify")

        history_for_classification = self.short_term_memory.get_history(thread_id, last_n=11)[:-1]
        previous_classification = self._last_classifications.get(thread_id)
        with timed_span(
            "classify.turn",
            trace_id=getattr(queue, "trace_id", ""),
            attributes={"thread_id": thread_id},
        ):
            classification = await self.classifier.aclassify_streaming(
                user_message,
                queue,
                history=history_for_classification,
                previous=previous_classification,
            )
        self._last_classifications[thread_id] = classification

        if classification.intent == "general":
            # 只认实义品类/品牌词；不能再让"预算3000"这种钱数把闲聊判成购物。
            if signals.has_strong_shopping_signal(user_message):
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

        # 装备清单安全网：露营/出差/带什么这类请求要走行程+装备链路，
        # 不能被“帐篷/睡袋”等商品词带到购物子图。
        if classification.intent != "customer_service" and is_gear_request(user_message):
            classification.intent = "travel"
            classification.primary_intent = "travel"
            classification.intents = ["travel"]
            classification.route = "plan_and_execute"
            classification.sub_intent = "gear_plan"
            classification.retrieval = "mixed"
            classification.reason = f"{classification.reason} | 装备清单安全网".strip(" |")
            self._last_classifications[thread_id] = classification
            await queue.emit(EventType.CLASSIFY, {
                "route": classification.route,
                "intent": "travel",
                "sub_intent": "gear_plan",
                "retrieval": classification.retrieval,
                "confidence": classification.confidence,
                "reason": classification.reason,
                "method": "keyword",
            }, step="classify")

        return classification

    def get_chat_history(self, thread_id: str) -> List[Any]:
        return self.short_term_memory.get_langchain_messages(thread_id, last_n=10)

    async def synthesize_composite_response(
        self,
        user_message: str,
        shopping: Dict[str, Any],
        travel: Dict[str, Any],
        constraints: SharedConstraints,
        queue: EventSink,
    ) -> str:
        """合并并约束校验双意图结果；单分支失败仍保留可验证的另一分支。"""
        def fallback() -> str:
            parts = []
            if shopping.get("status") == "ok":
                parts.append("【购物推荐】\n" + str(shopping.get("response") or "").strip())
            else:
                parts.append("【购物推荐】\n未能完成：" + str(shopping.get("error") or "未知错误"))
            if travel.get("status") == "ok":
                parts.append("【旅行计划】\n" + str(travel.get("response") or "").strip())
            else:
                parts.append("【旅行计划】\n未能完成：" + str(travel.get("error") or "未知错误"))
            return "\n\n".join(parts).strip()

        if shopping.get("status") != "ok" or travel.get("status") != "ok":
            return fallback()
        prompt = ChatPromptTemplate.from_messages([
            ("system", """把购物建议与旅行计划综合成一份一致的最终回答。
必须保留真实商品名和行程事实，不得新增未经检索的数据。
统一检查预算、日期、人数和装备/活动是否冲突；失败时保留原分支正文并明确说明。"""),
            ("user", """原始请求：{request}

共享约束：
{constraints}

购物分支：
{shopping}

旅行分支：
{travel}

请输出最终中文回答。"""),
        ])
        chain = prompt | self.llm
        try:
            result = await chain.ainvoke({
                "request": user_message,
                "constraints": constraints.prompt_context(),
                "shopping": shopping.get("response") or "",
                "travel": travel.get("response") or "",
            })
            synthesized = str(result.content or "").strip()
            return synthesized or fallback()
        except Exception as exc:
            log_exception("composite.synthesis", exc)
            await queue.emit_error(f"跨场景综合失败，已保留两个分支结果：{type(exc).__name__}", step="generate")
            return fallback()

    async def plan_shopping_retrieval(
        self,
        user_message: str,
        classification: ClassificationResult,
        queue: EventSink,
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
        queue: EventSink,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """购物子图节点：执行 SQL/RAG 检索。"""
        return await self._smart_retrieve(
            user_message,
            planned_strategy,
            queue,
            needs=needs,
            owner=user_id,
        )

    async def generate_shopping_response(
        self,
        user_message: str,
        user_id: str,
        thread_id: str,
        classification: ClassificationResult,
        retrieval: Dict[str, Any],
        queue: EventSink,
        validation_feedback: str = "",
        constraints: Optional[Dict[str, Any]] = None,
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
            memory_context = "\n".join(
                [m["content"] for m in cross_memories[:3]]
            )
        weather_context = await self.travel_service.get_weather_context(user_message)

        full_input = user_message
        if retrieval.get("product_context"):
            full_input += f"\n\n【商品信息】\n{retrieval['product_context']}"
        if retrieval.get("review_context"):
            full_input += f"\n\n【评价/口碑信息】\n{retrieval['review_context']}"
        if retrieval.get("diagnostics"):
            diag_text = "\n".join([f"- {d}" for d in retrieval["diagnostics"]])
            full_input += f"\n\n【检索诊断信息】\n{diag_text}"
        if weather_context:
            full_input += f"\n\n{weather_context}"
        if memory_context:
            full_input += (
                "\n\n【用户长期 Memory】\n"
                "以下内容来自已保存的用户 Memory，可用于个性化，不得否认或改写：\n"
                f"{memory_context}"
            )
        if constraints:
            constraint_text = "\n".join(f"- {key}: {value}" for key, value in constraints.items() if value not in (None, "", []))
            if constraint_text:
                full_input += f"\n\n【共享约束】\n{constraint_text}\n购物推荐不得突破预算、日期、人数等共享约束。"
        if validation_feedback:
            full_input += (
                "\n\n【上一版回答未通过校验，请修正】\n"
                f"{validation_feedback}\n"
                "请基于同一检索结果重新生成，只修正校验问题，不要编造新数据。"
            )

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

【真实数据规则】
- 天气和路线只能引用【真实工具证据】；失败、过期或来源不明的数据不能当作当前或实时信息；
- 如果【天气工具状态】说明数据不可用，必须明确说未获取到可验证的实时天气，不得给出猜测温度、季节典型值或虚构天气；
- 【用户长期 Memory】是真实的已保存偏好，应按用户需求自然使用；与本轮明确要求冲突时以本轮要求为准。

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
        with timed_span(
            "model.shopping_generate",
            trace_id=getattr(queue, "trace_id", ""),
            attributes={"user_id": user_id},
        ) as measurement:
            async for chunk in chain.astream({
                "chat_history": chat_history,
                "input": full_input
            }):
                if chunk.content:
                    full_response += chunk.content
                    await queue.emit_token(chunk.content, step="generate")
                measurement.add_attributes(extract_model_usage(chunk))
        return full_response

    @staticmethod
    def validate_shopping_response(response: str, retrieval: Dict[str, Any]) -> None:
        """购物子图节点：校验空回答、证据缺口披露和已知商品一致性。"""
        if not response or not response.strip():
            raise ValueError("购物回答为空")
        if not isinstance(retrieval, dict):
            raise ValueError("检索结果格式错误")

        response = response.strip()
        needs = retrieval.get("needs") or {}
        if retrieval.get("unanswerable"):
            disclosed = any(marker in response for marker in ("无法", "不完整", "不可用", "失败"))
            if not disclosed:
                raise ValueError("必需证据不可用时必须明确披露无法完成完整回答")
        if retrieval.get("partial"):
            disclosed = any(marker in response for marker in ("部分", "不完整", "暂未", "缺少", "无法"))
            if not disclosed:
                raise ValueError("部分证据缺失时必须明确披露信息不完整")
        product_context = str(retrieval.get("product_context") or "")
        review_context = str(retrieval.get("review_context") or "")
        diagnostics = [str(item) for item in retrieval.get("diagnostics") or []]
        products = retrieval.get("products") or []

        if needs.get("needs_product") and not product_context:
            if "暂未找到符合条件的商品" not in response:
                raise ValueError("缺少商品证据时必须明确回答暂未找到符合条件的商品")
        if product_context and "暂未找到符合条件的商品" in response:
            raise ValueError("已检索到商品证据，不应声称暂未找到商品")

        review_failed = (
            not review_context
            and any(
                ("RAG" in item and ("失败" in item or "未返回" in item))
                or ("评价" in item and ("失败" in item or "未返回" in item))
                for item in diagnostics
            )
        )
        if needs.get("needs_review") and review_failed:
            disclosed = "评价" in response and any(
                marker in response
                for marker in ("不完整", "未找到", "暂未", "无法", "失败", "没有")
            )
            if not disclosed:
                raise ValueError("评价检索失败时必须明确披露评价信息不完整")

        known_names = [
            str(product.get("name", "")).strip()
            for product in products
            if isinstance(product, dict) and str(product.get("name", "")).strip()
        ]
        if product_context and known_names and not any(
            name in response for name in known_names
        ):
            raise ValueError("回答必须至少引用一个检索到的真实商品名称")

    def _classify_data_needs(self, query: str, classification: 'ClassificationResult') -> dict:
        """按事实需求选择 SQL/RAG。

        两路证据：关键词（本函数）和分类器给的 ``retrieval`` 先验。
        关键词没看出需求时采信先验；关键词看出了就不让先验覆盖——
        词表是对当前这句原话的直接观察，比一次模型判断更可靠。
        """
        q = (query or '').lower()
        review_signals = sum(1 for kw in lexicon.RETRIEVAL_REVIEW if kw in q)
        attribute_signals = sum(1 for kw in lexicon.RETRIEVAL_ATTRIBUTE if kw in q)
        product_action_signals = sum(1 for kw in lexicon.RETRIEVAL_PRODUCT_ACTION if kw in q)

        if classification.intent == 'customer_service':
            # 订单/客服应由专门 Repository 处理，不能误判成商品证据检索。
            needs_product = False
            needs_review = review_signals > 0
        elif classification.intent == 'shopping':
            needs_product = attribute_signals > 0 or product_action_signals > 0
            needs_review = review_signals > 0
            # 纯评价/体验问题不需要商品 SQL；只有明确商品动作或结构化约束才查目录。
            if review_signals and not attribute_signals and not product_action_signals:
                needs_product = False
        elif classification.intent == 'travel':
            needs_product = False
            needs_review = True
        else:
            needs_product = False
            needs_review = False

        # 关键词没给出任何需求时，采信分类器先验（模型读懂了而词表没覆盖）
        if not needs_product and not needs_review:
            prior = str(getattr(classification, 'retrieval', '') or '')
            if classification.intent not in ('customer_service', 'general'):
                if prior in {'sql_only', 'mixed'}:
                    needs_product = True
                if prior in {'rag_only', 'mixed'}:
                    needs_review = True

        if needs_product and needs_review:
            strategy = 'mixed'
        elif needs_product:
            strategy = 'sql_only'
        elif needs_review:
            strategy = 'rag_only'
        else:
            strategy = 'none'

        facts = []
        required_sources = []
        if needs_product:
            facts.extend(['product_catalog', 'product_constraints'])
            required_sources.append('sql')
        if needs_review:
            facts.append('review_sentiment')
            required_sources.append('rag')
        return {
            'needs_review': needs_review,
            'needs_product': needs_product,
            'strategy': strategy,
            'facts': facts,
            'required_sources': required_sources,
            'optional_sources': [],
            'allow_partial': True,
            'source_policy': 'independent_not_fallback',
        }

    async def _emit_retrieval_plan(self, queue: 'EventSink', strategy: str, needs: dict, source: str = 'policy'):
        """检索规划信息已并入 Step 2「数据检索」描述，避免重复推送 Step 2。"""
        return

    async def _smart_retrieve(
        self,
        query: str,
        strategy: str,
        queue: EventSink,
        needs: dict = None,
        owner: Optional[str] = None,
    ) -> dict:
        """智能检索 - 统一委托 RetrievalService，编排层只处理事件和上下文。"""
        experiment_logger = getattr(self, "_experiment_logger", None)
        rerank_variant = None
        if experiment_logger is not None and getattr(self, "_reranker", None) is not None:
            rerank_variant = experiment_logger.assign_variant(owner or "anonymous", query)
        retrieve_kwargs = {
            "strategy": strategy,
            "top_k": 5,
            "needs": needs,
            "owner": owner,
            "concurrent": True,
            "trace_id": getattr(queue, "trace_id", ""),
        }
        if rerank_variant is not None:
            retrieve_kwargs["rerank_variant"] = rerank_variant
        retrieval = await asyncio.to_thread(
            self.retrieval_service.retrieve,
            query,
            **retrieve_kwargs,
        )
        if experiment_logger is not None and rerank_variant is not None:
            product_ids = [
                str(item.get("id"))
                for item in retrieval.get("products", [])
                if isinstance(item, dict) and item.get("id") is not None
            ]
            document_ids = [
                str(item.get("metadata", {}).get("document_id") or item.get("metadata", {}).get("id") or index)
                for index, item in enumerate(retrieval.get("documents", []))
            ]
            experiment_logger.log_impression(
                user_id=owner or "anonymous",
                session_id=getattr(queue, "trace_id", "") or "session",
                query=query,
                variant=rerank_variant,
                ranked_document_ids=product_ids + document_ids,
                metadata={"strategy": strategy, "selected_sources": retrieval.get("selected_sources", [])},
            )

        selected_sources = set(retrieval.get("selected_sources") or [])
        if "sql" in selected_sources:
            await queue.emit_tool_call("nl2sql", retrieval.get("sql", ""), step="retrieval")
        if "rag" in selected_sources:
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
                    f"{product.get('brand', '')} | 分类: {product.get('subcategory', '')} | "
                    f"库存: {product.get('stock', '')} | {str(product.get('description', ''))[:50]}"
                )

        review_parts = []
        if retrieval.get("documents"):
            review_parts.append("评价/口碑信息：")
            for document in retrieval["documents"][:3]:
                review_parts.append(f"- {document['content'][:100]}")

        return {
            'product_context': '\n'.join(product_parts).strip(),
            'review_context': '\n'.join(review_parts).strip(),
            'products': retrieval.get("products", [])[:5],
            'diagnostics': retrieval.get("diagnostics", []),
            'rag_ok': retrieval.get("rag_ok"),
            'rag_error': retrieval.get("rag_error"),
            'strategy': retrieval.get("strategy", strategy),
            'effective_strategy': retrieval.get("effective_strategy", strategy),
            'selected_sources': retrieval.get("selected_sources", []),
            'source_status': retrieval.get("source_status", {}),
            'source_errors': retrieval.get("source_errors", {}),
            'source_timings_ms': retrieval.get("source_timings_ms", {}),
            'source_timeout_seconds': retrieval.get("source_timeout_seconds"),
            'evidence_coverage': retrieval.get("evidence_coverage"),
            'missing_evidence': retrieval.get("missing_evidence", []),
            'partial': retrieval.get("partial", False),
            'unanswerable': retrieval.get("unanswerable", False),
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

# === 全局实例 ===
_turn_services = None


def get_turn_services(model_name: str = None) -> TurnServices:
    global _turn_services
    if _turn_services is None:
        _turn_services = TurnServices(model_name)
    return _turn_services
