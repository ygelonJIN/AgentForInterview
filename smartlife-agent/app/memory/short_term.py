"""
短期记忆 - 会话级对话历史
"""
from threading import RLock
from typing import List, Dict, Any, Optional
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from datetime import datetime


class ShortTermMemory:
    """短期记忆管理器"""

    def __init__(self, max_messages: int = 50):
        self.conversations: Dict[str, List[Dict[str, Any]]] = {}
        self.max_messages = max_messages
        self._lock = RLock()

    def add_message(self, session_id: str, role: str, content: str, metadata: Dict = None):
        """添加消息"""
        if not session_id:
            raise ValueError("session_id 不能为空")
        with self._lock:
            history = self.conversations.setdefault(session_id, [])
            message = {
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "metadata": metadata or {},
            }
            history.append(message)

            # 超过最大消息数时裁剪
            if len(history) > self.max_messages:
                self.conversations[session_id] = history[-self.max_messages :]

    def get_history(self, session_id: str, last_n: int = None) -> List[Dict[str, Any]]:
        """获取对话历史"""
        with self._lock:
            history = list(self.conversations.get(session_id, []))
        if last_n:
            return history[-last_n:]
        return history

    def get_langchain_messages(self, session_id: str, last_n: int = None) -> List:
        """转换为 LangChain 消息格式"""
        history = self.get_history(session_id, last_n)
        messages = []
        for msg in history:
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            elif msg["role"] == "assistant":
                messages.append(AIMessage(content=msg["content"]))
            elif msg["role"] == "system":
                messages.append(SystemMessage(content=msg["content"]))
        return messages

    def clear(self, session_id: str):
        """清除会话历史"""
        with self._lock:
            self.conversations.pop(session_id, None)

    def get_summary(self, session_id: str) -> str:
        """获取会话摘要"""
        history = self.get_history(session_id)
        if not history:
            return "暂无对话记录"
        return f"共 {len(history)} 条消息，最近: {history[-1]['content'][:100]}"
