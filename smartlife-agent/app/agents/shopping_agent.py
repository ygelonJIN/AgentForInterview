"""
[COMPATIBILITY] Shopping Agent 的旧同步实现。

当前流式主链路由 OrchestratorV2 + RetrievalService 处理。
"""
from typing import Dict, List, Any, Optional
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from app.config import create_llm
# Agent imports removed - using direct LLM invocation
from app.retrieval.fusion import HybridRetriever
from app.memory.long_term import LongTermMemory

class ShoppingAgent:
    """购物 Agent - ReAct 模式"""
    
    def __init__(self, model_name: str = None):
        self.llm = create_llm(model_name, max_tokens=2048)
        self._hybrid_retriever = None
        self._memory = None
        
        self.导购_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个智能导购助手，帮用户找到最合适的商品。

能力：
1. 使用 NL2SQL 查询商品数据库（价格、分类、品牌等结构化条件）
2. 使用 RAG 检索用户评价（质量、口碑等语义条件）
3. 跨场景推荐：参考用户之前的旅行/购物偏好

工作流程：
1. 理解用户需求，区分结构化条件和语义条件
2. 调用混合检索（NL2SQL + RAG + Rerank）获取结果
3. 综合推荐，说明推荐理由

回复要求：
- 用中文回复
- 推荐时给出具体商品名、价格、推荐理由
- 如果有用户评价，引用关键评价内容
- 预算敏感时优先推荐性价比高的商品"""),
            MessagesPlaceholder(variable_name="chat_history"),
            ("user", "{input}")
        ])
        
        self.客服_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个智能客服，帮用户解决购物相关问题。

能力：
1. 查询订单状态
2. 解答退换货政策
3. 推荐相关商品
4. 处理投诉和建议

退换货政策：
- 7天无理由退换货
- 15天内质量问题免费换货
- 退货运费由买家承担（质量问题除外）

回复要求：
- 态度友好、专业
- 尽量一次性解决问题
- 需要查询时主动使用工具"""),
            MessagesPlaceholder(variable_name="chat_history"),
            ("user", "{input}")
        ])
    
    @property
    def hybrid_retriever(self):
        if self._hybrid_retriever is None:
            try:
                self._hybrid_retriever = HybridRetriever()
            except Exception:
                self._hybrid_retriever = None
        return self._hybrid_retriever
    
    @property
    def memory(self):
        if self._memory is None:
            try:
                self._memory = LongTermMemory()
            except Exception:
                self._memory = None
        return self._memory
    
    def process(self, user_input: str, user_id: str = "user_001", chat_history: List = None) -> Dict[str, Any]:
        """处理用户请求"""
        if chat_history is None:
            chat_history = []
        
        # 获取跨场景记忆
        cross_memories = self.memory.get_cross_scene_memories(
            user_id,
            "shopping",
            query=user_input,
        ) if self.memory else []
        memory_context = ""
        if cross_memories:
            memory_context = "\n用户历史偏好：\n" + "\n".join([m["content"] for m in cross_memories[:3]])
        
        # 混合检索
        retrieval_result = self.hybrid_retriever.search(user_input) if self.hybrid_retriever else {"products": [], "rag_results": []}
        
        # 构建上下文
        context = ""
        if retrieval_result.get("products"):
            context += "\n检索到的商品：\n"
            for p in retrieval_result["products"][:5]:
                if isinstance(p, dict) and "name" in p:
                    context += f"- {p['name']} ¥{p.get('price', 'N/A')} {p.get('description', '')}\n"
                elif isinstance(p, dict) and "rag_content" in p:
                    context += f"- {p['rag_content'][:100]}\n"
        
        if memory_context:
            context += memory_context
        
        # 使用 LLM 生成回复
        messages = self.导购_prompt.format_messages(
            chat_history=chat_history,
            input=user_input + (f"\n\n参考信息：{context}" if context else "")
        )
        
        response = self.llm.invoke(messages)
        
        return {
            "response": response.content,
            "retrieval": retrieval_result,
            "memories_used": len(cross_memories),
            "agent": "shopping"
        }
