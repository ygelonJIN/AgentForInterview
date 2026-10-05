"""
[DEPRECATED] 旧版 OrchestratorAgent。

当前主链路是 `app/main.py -> app.agents.langgraph_orchestrator`。
本文件仅保留兼容调用，不应新增业务逻辑。
"""
import sys
import os
from typing import Dict, List, Any, Optional
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from app.config import create_llm
from langchain_core.prompts import ChatPromptTemplate
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.router import TaskRouter, TaskRoute
from app.mcp_servers.shopping_server import ShoppingMCPServer
from app.mcp_servers.travel_server import TravelMCPServer
from app.mcp_servers.memory_server import MemoryMCPServer
from app.agents.shopping_agent import ShoppingAgent
from app.agents.travel_agent import TravelAgent
from app.agents.negotiation_agent import NegotiationAgent
from app.agents.reflection import ReflectionAgent
from app.tools import get_all_tools
from app.retrieval.fusion import HybridRetriever
from app.memory.short_term import ShortTermMemory
from app.memory.long_term import LongTermMemory
from app.memory.compressor import MemoryCompressor

class OrchestratorAgent:
    """主协调器 Agent"""
    
    def __init__(self, model_name: str = None):
        self.llm = create_llm(model_name if model_name else None)
        self.router = TaskRouter()
        
        # 子 Agents
        self.shopping_agent = ShoppingAgent(model_name)
        self.travel_agent = TravelAgent(model_name)
        self.negotiation_agent = NegotiationAgent()
        self.reflection_agent = ReflectionAgent(model_name)
        
        # MCP Servers
        self.shopping_server = ShoppingMCPServer()
        self.travel_server = TravelMCPServer()
        self.memory_server = MemoryMCPServer()
        
        # 工具
        self.all_tools = get_all_tools()
        
        # 检索和记忆（懒加载，避免启动时 Embeddings 失败）
        self._hybrid_retriever = None
        self._short_term_memory = None
        self._long_term_memory = None
        self._compressor = None
        
        # 意图识别 prompt
        self.intent_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个意图识别专家。分析用户消息，识别意图类别。

意图类别：
- shopping: 购物相关（商品搜索、导购、评价）
- customer_service: 客服相关（订单查询、退换货、投诉）
- travel: 旅游相关（行程规划、目的地推荐、酒店查询）
- negotiation: 社交协商（多人出行、偏好协调）
- general: 通用对话

输出JSON格式：
{{"intent": "类别", "sub_intent": "子意图", "confidence": 0.9}}"""),
            ("user", "{message}")
        ])
    
    @property
    def hybrid_retriever(self):
        if self._hybrid_retriever is None:
            try:
                self._hybrid_retriever = HybridRetriever()
            except Exception as e:
                print(f"Warning: RAG 初始化失败: {e}")
                self._hybrid_retriever = None
        return self._hybrid_retriever
    
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
                self._long_term_memory = None
        return self._long_term_memory
    
    @property
    def compressor(self):
        if self._compressor is None:
            try:
                self._compressor = MemoryCompressor()
            except Exception:
                self._compressor = None
        return self._compressor
    
    async def process(self, user_message: str, user_id: str = "user_001", session_id: str = "default") -> Dict[str, Any]:
        """处理用户消息"""
        # 1. 保存到短期记忆
        self.short_term_memory.add_message(session_id, "user", user_message)
        
        # 2. 意图识别
        intent_chain = self.intent_prompt | self.llm
        intent_result = intent_chain.invoke({"message": user_message})
        try:
            intent = json.loads(intent_result.content)
        except:
            intent = {"intent": "general", "sub_intent": "chat", "confidence": 0.5}
        
        # 3. 路由到合适的处理器
        route = self.router.route(user_message)
        
        chat_history = self.short_term_memory.get_langchain_messages(session_id, last_n=10)
        
        response = None
        
        if intent["intent"] == "shopping":
            response = self.shopping_agent.process(user_message, user_id, chat_history)
        elif intent["intent"] == "customer_service":
            response = self.shopping_agent.process(user_message, user_id, chat_history)
        elif intent["intent"] == "travel":
            response = self.travel_agent.plan_trip(user_message, user_id)
        elif intent["intent"] == "negotiation":
            # 解析参与者信息
            response = {"response": "请提供参与者信息（用户ID和偏好）", "agent": "negotiation"}
        else:
            # 通用对话
            general_prompt = ChatPromptTemplate.from_messages([
                ("system", "你是 SmartLife Agent 智能助手，帮助用户购物和旅行规划。用中文回复。"),
                *[(msg["role"], msg["content"]) for msg in self.short_term_memory.get_history(session_id, last_n=5)],
                ("user", user_message)
            ])
            chain = general_prompt | self.llm
            result = chain.invoke({})
            response = {"response": result.content, "agent": "general"}
        
        # 4. 保存回复到记忆
        assistant_msg = response.get("response", response.get("plan", str(response)))
        self.short_term_memory.add_message(session_id, "assistant", assistant_msg)
        
        # 5. 定期压缩记忆
        history = self.short_term_memory.get_history(session_id)
        if len(history) % 20 == 0 and len(history) > 0:
            summary = self.compressor.compress(history)
            if self.long_term_memory: self.long_term_memory.save_summary(user_id, summary)
        
        # 6. 保存重要事件到长期记忆
        if intent["intent"] in ["shopping", "travel"]:
            if self.long_term_memory: self.long_term_memory.save_event(
                user_id,
                event_type=intent["intent"],
                event_data={"action": "对话", "object": user_message[:50], "scene": intent["intent"]}
            )
        
        return {
            "response": assistant_msg,
            "intent": intent,
            "route": route.model_dump() if hasattr(route, 'model_dump') else {"route": route.route, "reason": route.reason},
            "agent_used": response.get("agent", "unknown"),
            "memories": {
                "short_term_count": len(self.short_term_memory.get_history(session_id)),
                "cross_scene_memories": len(self.long_term_memory.get_cross_scene_memories(user_id, intent["intent"])) if self.long_term_memory else 0
            }
        }
    
    def get_session_summary(self, session_id: str) -> str:
        """获取会话摘要"""
        return self.short_term_memory.get_summary(session_id)

# 全局实例
_orchestrator = None

def get_orchestrator(model_name: str = None) -> OrchestratorAgent:
    """获取全局 Orchestrator 实例"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = OrchestratorAgent(model_name)
    return _orchestrator

async def process_user_message(message: str, user_id: str = "user_001", session_id: str = "default") -> Dict[str, Any]:
    """处理用户消息的便捷函数"""
    orchestrator = get_orchestrator()
    return await orchestrator.process(message, user_id, session_id)
