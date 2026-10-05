"""
Negotiation Agent - 社交协商 Agent
"""
from typing import Dict, List, Any
from app.negotiation.graph import NegotiationGraph
from app.negotiation.preference import PreferenceAnalyzer
from app.memory.long_term import LongTermMemory

class NegotiationAgent:
    """社交协商 Agent"""
    
    def __init__(self):
        self.graph = NegotiationGraph()
        self.preference_analyzer = PreferenceAnalyzer()
        self.memory = LongTermMemory()
    
    def negotiate(self, participants: List[Dict[str, Any]], scenario: str = "travel") -> Dict[str, Any]:
        """
        执行社交协商
        
        Args:
            participants: 参与者列表，每个包含 user_id, preferences 等
            travel_history: 旅行历史
            scenario: 场景类型 travel/shopping
        """
        enriched_participants = self.enrich_participants(participants, scenario)
        
        # 执行协商
        result = self.graph.negotiate(enriched_participants, scenario=scenario)
        
        return {
            "participants": len(participants),
            "preferences": result.get("preferences", []),
            "common_ground": result.get("common_ground", {}),
            "conflicts": result.get("conflicts", []),
            "compromise": result.get("compromise", {}),
            "plan": result.get("plan", {}),
            "plan_text": result.get("plan_text", ""),
            "status": result.get("status", "unknown"),
            "messages": result.get("messages", []),
            "agent": "negotiation"
        }

    def enrich_participants(
        self,
        participants: List[Dict[str, Any]],
        scenario: str = "travel",
    ) -> List[Dict[str, Any]]:
        """按独立 user_id 查询记忆，补充参与者历史。"""
        enriched_participants = []
        for p in participants:
            user_id = p.get("user_id", "unknown")
            memories = self.memory.get_cross_scene_memories(
                user_id,
                scenario,
                query=scenario,
            )
            enriched = {
                **p,
                "purchase_history": [m["content"] for m in memories if "购物" in m.get("content", "") or "购买" in m.get("content", "")],
                "travel_history": [m["content"] for m in memories if "旅行" in m.get("content", "") or "景点" in m.get("content", "")]
            }
            enriched_participants.append(enriched)
        return enriched_participants
