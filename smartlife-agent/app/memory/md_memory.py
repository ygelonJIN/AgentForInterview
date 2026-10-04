"""
MD 文档记忆 - 纯自然语言文档存储

特点：
- 人类可读可编辑
- 作为权威数据源
- 与向量库同步
"""
import os
import json
import re
from typing import List, Dict, Any, Optional
from datetime import datetime


class MDMemory:
    """MD 文档记忆管理器"""

    def __init__(self, base_dir: str = None):
        if base_dir is None:
            base_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "memory_docs"
            )
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _get_user_dir(self, user_id: str) -> str:
        """获取用户记忆目录"""
        user_dir = os.path.join(self.base_dir, user_id)
        os.makedirs(user_dir, exist_ok=True)
        return user_dir

    def _get_preferences_path(self, user_id: str) -> str:
        """获取偏好文件路径"""
        return os.path.join(self._get_user_dir(user_id), "preferences.md")

    def _get_events_path(self, user_id: str) -> str:
        """获取事件文件路径"""
        return os.path.join(self._get_user_dir(user_id), "events.md")

    def _get_index_path(self, user_id: str) -> str:
        """获取索引文件路径"""
        return os.path.join(self._get_user_dir(user_id), "index.json")

    def _load_index(self, user_id: str) -> Dict[str, Any]:
        """加载索引"""
        index_path = self._get_index_path(user_id)
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"memories": [], "last_updated": None}

    def _save_index(self, user_id: str, index: Dict[str, Any]):
        """保存索引"""
        index_path = self._get_index_path(user_id)
        index["last_updated"] = datetime.now().isoformat()
        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def _generate_id(self) -> str:
        """生成唯一 ID"""
        import uuid
        return str(uuid.uuid4())[:8]

    def save_preference(self, user_id: str, preference: str, category: str = "general") -> str:
        """
        保存用户偏好
        
        Args:
            user_id: 用户 ID
            preference: 偏好内容（纯文本）
            category: 偏好类别（shopping/travel/general）
            
        Returns:
            记忆 ID
        """
        memory_id = self._generate_id()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # 读取现有内容
        prefs_path = self._get_preferences_path(user_id)
        if os.path.exists(prefs_path):
            with open(prefs_path, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            content = "# 用户偏好\n\n"
        
        # 追加新偏好
        entry = f"\n## [{memory_id}] {category} - {timestamp}\n\n{preference}\n"
        content += entry
        
        # 保存
        with open(prefs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # 更新索引
        index = self._load_index(user_id)
        index["memories"].append({
            "id": memory_id,
            "type": "preference",
            "category": category,
            "timestamp": timestamp,
            "content": preference
        })
        self._save_index(user_id, index)
        
        return memory_id

    def save_event(self, user_id: str, event: str, event_type: str = "general") -> str:
        """
        保存事件记录
        
        Args:
            user_id: 用户 ID
            event: 事件内容（纯文本）
            event_type: 事件类型（shopping/travel/general）
            
        Returns:
            记忆 ID
        """
        memory_id = self._generate_id()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # 读取现有内容
        events_path = self._get_events_path(user_id)
        if os.path.exists(events_path):
            with open(events_path, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            content = "# 事件记录\n\n"
        
        # 追加新事件
        entry = f"\n## [{memory_id}] {event_type} - {timestamp}\n\n{event}\n"
        content += entry
        
        # 保存
        with open(events_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # 更新索引
        index = self._load_index(user_id)
        index["memories"].append({
            "id": memory_id,
            "type": "event",
            "event_type": event_type,
            "timestamp": timestamp,
            "content": event
        })
        self._save_index(user_id, index)
        
        return memory_id

    def delete_memory(self, user_id: str, memory_id: str) -> bool:
        """
        删除记忆
        
        Args:
            user_id: 用户 ID
            memory_id: 记忆 ID
            
        Returns:
            是否删除成功
        """
        index = self._load_index(user_id)
        
        # 找到记忆
        memory = None
        for m in index["memories"]:
            if m["id"] == memory_id:
                memory = m
                break
        
        if not memory:
            return False
        
        # 从索引中删除
        index["memories"] = [m for m in index["memories"] if m["id"] != memory_id]
        self._save_index(user_id, index)
        
        # 从 MD 文件中删除
        if memory["type"] == "preference":
            file_path = self._get_preferences_path(user_id)
        else:
            file_path = self._get_events_path(user_id)
        
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 删除对应段落
            pattern = rf"\n## \[{memory_id}\].*?(?=\n## \[|$)"
            content = re.sub(pattern, "", content, flags=re.DOTALL)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
        
        return True

    def get_all_memories(self, user_id: str) -> List[Dict[str, Any]]:
        """获取所有记忆"""
        index = self._load_index(user_id)
        return index.get("memories", [])

    def search_memories(self, user_id: str, keyword: str) -> List[Dict[str, Any]]:
        """搜索记忆（关键词匹配）"""
        memories = self.get_all_memories(user_id)
        results = []
        for m in memories:
            if keyword.lower() in m.get("content", "").lower():
                results.append(m)
        return results

    def get_memory_content(self, user_id: str, memory_id: str) -> Optional[str]:
        """获取记忆内容"""
        index = self._load_index(user_id)
        for m in index["memories"]:
            if m["id"] == memory_id:
                return m.get("content")
        return None

    def update_memory(self, user_id: str, memory_id: str, new_content: str) -> bool:
        """
        更新记忆内容
        
        Args:
            user_id: 用户 ID
            memory_id: 记忆 ID
            new_content: 新内容
            
        Returns:
            是否更新成功
        """
        index = self._load_index(user_id)
        
        # 找到记忆
        memory = None
        for m in index["memories"]:
            if m["id"] == memory_id:
                memory = m
                break
        
        if not memory:
            return False
        
        # 更新索引
        for m in index["memories"]:
            if m["id"] == memory_id:
                m["content"] = new_content
                break
        self._save_index(user_id, index)
        
        # 更新 MD 文件
        if memory["type"] == "preference":
            file_path = self._get_preferences_path(user_id)
        else:
            file_path = self._get_events_path(user_id)
        
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 替换对应段落
            pattern = rf"\n## \[{memory_id}\].*?(?=\n## \[|$)"
            replacement = f"\n## [{memory_id}] {memory.get('category', memory.get('event_type', 'general'))} - {memory['timestamp']}\n\n{new_content}\n"
            content = re.sub(pattern, replacement, content, flags=re.DOTALL)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
        
        return True

    def get_user_profile(self, user_id: str) -> Dict[str, Any]:
        """获取用户画像"""
        memories = self.get_all_memories(user_id)
        preferences = [m for m in memories if m["type"] == "preference"]
        events = [m for m in memories if m["type"] == "event"]
        
        return {
            "user_id": user_id,
            "memory_count": len(memories),
            "preference_count": len(preferences),
            "event_count": len(events),
            "recent_memories": memories[-5:] if memories else []
        }
