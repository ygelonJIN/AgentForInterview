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
            "content": preference,
            "version": 1,
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
            "content": event,
            "version": 1,
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

    def get_raw_documents(self, user_id: str) -> str:
        """原样返回长期 MD 记忆文档，不做摘要或压缩。"""
        documents = []
        for path in (
            self._get_preferences_path(user_id),
            self._get_events_path(user_id),
        ):
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if content.strip():
                    documents.append(content)
        return "\n\n".join(documents)

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
                m["version"] = int(m.get("version", 1)) + 1
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

    def _document_paths(self, user_id: str) -> Dict[str, str]:
        return {
            "preferences": self._get_preferences_path(user_id),
            "events": self._get_events_path(user_id),
        }

    def get_raw_document(self, user_id: str, document_type: str) -> str:
        """返回单个 MD 原文，供人工查看和编辑。"""
        paths = self._document_paths(user_id)
        if document_type not in paths:
            raise ValueError("document_type 必须是 preferences 或 events")
        path = paths[document_type]
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def write_raw_document(self, user_id: str, document_type: str, content: str) -> str:
        """写入 MD 原文；调用方随后应执行 repository.sync_from_markdown。"""
        paths = self._document_paths(user_id)
        if document_type not in paths:
            raise ValueError("document_type 必须是 preferences 或 events")
        path = paths[document_type]
        self._get_user_dir(user_id)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def parse_markdown_memories(self, user_id: str) -> List[Dict[str, Any]]:
        """从 MD 原文解析记忆；无 ID 的新段落会获得稳定 ID 并回写标题。"""
        parsed: List[Dict[str, Any]] = []
        seen_ids = set()
        exact_heading = re.compile(r"^##\s+\[([^\]]+)\]\s+(.+?)\s+-\s+(.+?)\s*$")
        manual_heading = re.compile(r"^##\s+(?!\[)(.+?)\s+-\s+(.+?)\s*$")

        for memory_type, file_path in (
            ("preference", self._get_preferences_path(user_id)),
            ("event", self._get_events_path(user_id)),
        ):
            if not os.path.exists(file_path):
                continue
            with open(file_path, "r", encoding="utf-8") as handle:
                original = handle.read()

            sections: List[Dict[str, Any]] = []
            current: Optional[Dict[str, Any]] = None
            preamble_lines: List[str] = []
            changed = False
            for line in original.splitlines():
                exact = exact_heading.match(line)
                manual = None if exact else manual_heading.match(line)
                if exact or manual:
                    if current is not None:
                        current["lines"].append("")
                        sections.append(current)
                    if exact:
                        memory_id, label, timestamp = exact.groups()
                    else:
                        label, timestamp = manual.groups()
                        memory_id = self._generate_id()
                        line = f"## [{memory_id}] {label} - {timestamp}"
                        changed = True
                    if memory_id in seen_ids:
                        raise ValueError(f"MD 中存在重复 memory_id: {memory_id}")
                    seen_ids.add(memory_id)
                    current = {
                        "id": memory_id,
                        "type": memory_type,
                        "label": label.strip(),
                        "timestamp": timestamp.strip(),
                        "lines": [],
                        "heading": line,
                    }
                elif current is not None:
                    current["lines"].append(line)
                elif not line.startswith("#"):
                    preamble_lines.append(line)

            if current is not None:
                current["lines"].append("")
                sections.append(current)

            preamble = "\n".join(preamble_lines).strip()
            if preamble:
                for block in re.split(r"\n\s*\n+", preamble):
                    content = block.strip()
                    if not content:
                        continue
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                    memory_id = self._generate_id()
                    changed = True
                    sections.append({
                        "id": memory_id,
                        "type": memory_type,
                        "label": "general",
                        "timestamp": timestamp,
                        "lines": [content],
                        "heading": f"## [{memory_id}] general - {timestamp}",
                    })

            for section in sections:
                category_key = "category" if memory_type == "preference" else "event_type"
                parsed.append({
                    "id": section["id"],
                    "type": memory_type,
                    category_key: section["label"],
                    "timestamp": section["timestamp"],
                    "content": "\n".join(section["lines"]).strip(),
                    "version": 1,
                })

            if changed:
                normalized_lines = ["# 用户偏好\n" if memory_type == "preference" else "# 事件记录\n"]
                for section in sections:
                    normalized_lines.extend([section["heading"], "", "\n".join(section["lines"]).strip(), ""])
                with open(file_path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(normalized_lines).rstrip() + "\n")
        return parsed

    def reconcile_index_from_markdown(self, user_id: str) -> Dict[str, Any]:
        """以 MD 原文为准重建 index.json，并保留/递增版本号。"""
        old_index = self._load_index(user_id)
        old_by_id = {item.get("id"): item for item in old_index.get("memories", [])}
        parsed = self.parse_markdown_memories(user_id)
        for item in parsed:
            previous = old_by_id.get(item["id"], {})
            item["version"] = int(previous.get("version", 1))
            if previous.get("content") != item.get("content"):
                item["version"] += 1
                item["vector_version"] = 0
            else:
                item["vector_version"] = int(previous.get("vector_version", 0))
        self._save_index(user_id, {"memories": parsed})
        return {
            "indexed": len(parsed),
            "added": len([item for item in parsed if item["id"] not in old_by_id]),
            "updated": len([
                item for item in parsed
                if item["id"] in old_by_id and old_by_id[item["id"]].get("content") != item.get("content")
            ]),
            "removed": len([memory_id for memory_id in old_by_id if memory_id not in {item["id"] for item in parsed}]),
        }

    def autofmt_document(
        self,
        document_type: str,
        edited_content: str,
        original_content: str = "",
    ) -> str:
        """把新增的自由文本或简单 Markdown 标题自动补成稳定记忆格式。"""
        if document_type not in {"preferences", "events"}:
            raise ValueError("document_type 必须是 preferences 或 events")
        base = (original_content or "").rstrip()
        if base and edited_content.rstrip().startswith(base):
            tail = edited_content.rstrip()[len(base):].strip("\n")
        elif not base:
            tail = edited_content.strip()
        else:
            return edited_content

        if not tail:
            return edited_content
        blocks = [block.strip() for block in re.split(r"\n\s*\n+", tail) if block.strip()]
        formatted = []
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for block in blocks:
            if re.match(r"^##\s+\[[^\]]+\]", block):
                formatted.append(block)
                continue
            lines = block.splitlines()
            if lines and lines[0].startswith("## "):
                title = lines[0][3:].strip()
                body = "\n".join(lines[1:]).strip()
                content = "\n\n".join(part for part in (title, body) if part)
            else:
                content = block
            memory_id = self._generate_id()
            category = "general" if document_type == "preferences" else "general"
            formatted.append(
                f"## [{memory_id}] {category} - {timestamp}\n\n{content}\n"
            )

        normalized_base = base or (
            "# 用户偏好\n" if document_type == "preferences" else "# 事件记录\n"
        )
        return normalized_base.rstrip() + "\n\n" + "\n\n".join(formatted) + "\n"

    def mark_vector_synced(self, user_id: str, memory_id: str, version: int) -> bool:
        """记录某一版本已经成功写入持久化向量库。"""
        index = self._load_index(user_id)
        changed = False
        for item in index.get("memories", []):
            if item.get("id") == memory_id:
                item["vector_version"] = int(version)
                changed = True
                break
        if changed:
            self._save_index(user_id, index)
        return changed

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
