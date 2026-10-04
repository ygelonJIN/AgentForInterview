"""
社交协商状态机 - LangGraph
"""
from typing import Dict, List, Any, TypedDict
from langgraph.graph import StateGraph, END
from app.negotiation.preference import PreferenceAnalyzer, UserPreference
from app.negotiation.conflict import ConflictResolver


class NegotiationState(TypedDict):
    """协商状态"""
    participants: List[Dict[str, Any]]
    preferences: List[Dict[str, Any]]
    common_ground: Dict[str, Any]
    conflicts: List[Dict[str, Any]]
    compromise: Dict[str, Any]
    status: str
    messages: List[str]


class NegotiationGraph:
    """社交协商图"""

    def __init__(self):
        self.preference_analyzer = PreferenceAnalyzer()
        self.conflict_resolver = ConflictResolver()
        self.graph = self._build_graph()

    def _build_graph(self) -> StateGraph:
        """构建状态图"""
        graph = StateGraph(NegotiationState)

        # 添加节点
        graph.add_node("analyze_preferences", self._analyze_preferences)
        graph.add_node("find_common_ground", self._find_common_ground)
        graph.add_node("identify_conflicts", self._identify_conflicts)
        graph.add_node("resolve_conflicts", self._resolve_conflicts)
        graph.add_node("generate_plan", self._generate_plan)

        # 定义边
        graph.set_entry_point("analyze_preferences")
        graph.add_edge("analyze_preferences", "find_common_ground")
        graph.add_edge("find_common_ground", "identify_conflicts")
        graph.add_conditional_edges(
            "identify_conflicts",
            self._has_conflicts,
            {True: "resolve_conflicts", False: "generate_plan"},
        )
        graph.add_edge("resolve_conflicts", "generate_plan")
        graph.add_edge("generate_plan", END)

        return graph.compile()

    def _analyze_preferences(self, state: NegotiationState) -> Dict:
        """分析每个参与者的偏好"""
        preferences = []
        for participant in state["participants"]:
            pref = self.preference_analyzer.analyze(
                user_id=participant.get("user_id", "unknown"),
                user_info=participant,
                purchase_history=participant.get("purchase_history"),
                travel_history=participant.get("travel_history"),
            )
            preferences.append(pref.model_dump())

        return {
            "preferences": preferences,
            "status": "preferences_analyzed",
            "messages": ["偏好分析完成"],
        }

    def _find_common_ground(self, state: NegotiationState) -> Dict:
        """找到共同点"""
        prefs = [UserPreference(**p) for p in state["preferences"]]
        common = self.preference_analyzer.find_common_ground(prefs)
        return {
            "common_ground": common,
            "status": "common_ground_found",
            "messages": [f"找到共同标签：{common.get('common_tags', [])}"],
        }

    def _identify_conflicts(self, state: NegotiationState) -> Dict:
        """识别冲突"""
        conflicts = self.conflict_resolver.identify_conflicts(state["preferences"])
        return {
            "conflicts": conflicts,
            "status": "conflicts_identified",
            "messages": [f"发现 {len(conflicts)} 个冲突"],
        }

    def _has_conflicts(self, state: NegotiationState) -> bool:
        """是否有冲突"""
        return len(state.get("conflicts", [])) > 0

    def _resolve_conflicts(self, state: NegotiationState) -> Dict:
        """解决冲突"""
        compromise = self.conflict_resolver.resolve(
            preferences=state["preferences"],
            conflicts=state["conflicts"],
            common_ground=state["common_ground"],
        )
        return {
            "compromise": compromise,
            "status": "conflicts_resolved",
            "messages": ["冲突已解决"],
        }

    def _generate_plan(self, state: NegotiationState) -> Dict:
        """生成最终方案"""
        return {
            "status": "completed",
            "messages": ["协商完成，已生成推荐方案"],
        }

    def negotiate(self, participants: List[Dict[str, Any]]) -> Dict[str, Any]:
        """执行协商流程"""
        initial_state: NegotiationState = {
            "participants": participants,
            "preferences": [],
            "common_ground": {},
            "conflicts": [],
            "compromise": {},
            "status": "started",
            "messages": ["协商开始"],
        }

        result = self.graph.invoke(initial_state)
        return result
