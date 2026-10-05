"""
社交协商状态机 - LangGraph
"""
from typing import Dict, List, Any, Optional, TypedDict
from langgraph.graph import StateGraph, END, START
from app.negotiation.preference import PreferenceAnalyzer, UserPreference
from app.negotiation.conflict import ConflictResolver


class NegotiationState(TypedDict):
    """协商状态"""
    participants: List[Dict[str, Any]]
    scenario: str
    preferences: List[Dict[str, Any]]
    common_ground: Dict[str, Any]
    conflicts: List[Dict[str, Any]]
    compromise: Dict[str, Any]
    plan: Dict[str, Any]
    plan_text: str
    status: str
    messages: List[str]


class NegotiationGraph:
    """社交协商图"""

    def __init__(self, preference_analyzer=None, conflict_resolver=None):
        self.preference_analyzer = preference_analyzer or PreferenceAnalyzer()
        self.conflict_resolver = conflict_resolver or ConflictResolver()
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
        graph.add_edge(START, "analyze_preferences")
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
            "messages": state.get("messages", []) + ["偏好分析完成"],
        }

    def _find_common_ground(self, state: NegotiationState) -> Dict:
        """找到共同点"""
        prefs = [UserPreference(**p) for p in state["preferences"]]
        common = self.preference_analyzer.find_common_ground(prefs)
        return {
            "common_ground": common,
            "status": "common_ground_found",
            "messages": state.get("messages", []) + [f"找到共同标签：{common.get('common_tags', [])}"],
        }

    def _identify_conflicts(self, state: NegotiationState) -> Dict:
        """识别冲突"""
        conflicts = self.conflict_resolver.identify_conflicts(state["preferences"])
        return {
            "conflicts": conflicts,
            "status": "conflicts_identified",
            "messages": state.get("messages", []) + [f"发现 {len(conflicts)} 个冲突"],
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
            "messages": state.get("messages", []) + ["冲突已解决"],
        }

    def _generate_plan(self, state: NegotiationState) -> Dict:
        """生成结构化、可执行的最终协商方案。"""
        common_ground = state.get("common_ground", {})
        preferences = state.get("preferences", [])
        conflicts = state.get("conflicts", [])
        compromise = state.get("compromise", {})
        tags = list(common_ground.get("common_tags", []))
        styles = sorted({
            style
            for preference in preferences
            for style in preference.get("travel_preferences", {}).get("preferred_styles", [])
        })
        budget = common_ground.get("min_budget", 0)
        activities = tags[:3] or ["自由活动"]
        plan = {
            "title": f"{state.get('scenario', 'travel')}协商方案",
            "participant_count": len(preferences),
            "common_tags": tags,
            "recommended_styles": styles,
            "budget_per_person": budget,
            "activities": activities,
            "conflicts": conflicts,
            "compromise": compromise,
            "decision_rule": "优先执行共同偏好；预算按最低可承受预算执行；剩余冲突由参与者投票确认。",
            "requires_confirmation": bool(conflicts),
        }
        lines = [
            f"## {plan['title']}",
            f"- 参与人数：{plan['participant_count']}",
            f"- 共同偏好：{'、'.join(tags) if tags else '暂无明确共同标签'}",
            f"- 推荐风格：{'、'.join(styles) if styles else '均衡安排'}",
            f"- 人均预算：{budget if budget else '待确认'}",
            f"- 推荐活动：{'、'.join(activities)}",
        ]
        if conflicts:
            lines.append(f"- 冲突处理：{compromise.get('compromise_plan', '采用共同偏好优先的折中方案')}")
            lines.append("- 后续动作：请参与者对冲突项投票确认。")
        else:
            lines.append("- 后续动作：可直接按共同偏好执行。")
        plan_text = "\n".join(lines)
        return {
            "status": "completed",
            "plan": plan,
            "plan_text": plan_text,
            "messages": state.get("messages", []) + ["协商完成，已生成推荐方案"],
        }

    @staticmethod
    def _initial_state(
        participants: List[Dict[str, Any]],
        scenario: str = "travel",
    ) -> NegotiationState:
        return {
            "participants": list(participants or []),
            "scenario": scenario,
            "preferences": [],
            "common_ground": {},
            "conflicts": [],
            "compromise": {},
            "plan": {},
            "plan_text": "",
            "status": "started",
            "messages": ["协商开始"],
        }

    def negotiate(
        self,
        participants: List[Dict[str, Any]],
        scenario: str = "travel",
    ) -> Dict[str, Any]:
        """执行协商流程"""
        return self.graph.invoke(self._initial_state(participants, scenario))

    async def run_streaming(
        self,
        participants: List[Dict[str, Any]],
        scenario: str = "travel",
        queue=None,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行协商流程并把中间状态推送到 EventQueue。"""
        result = await self.graph.ainvoke(
            self._initial_state(participants, scenario),
            config={"configurable": {"thread_id": thread_id or f"negotiation:{scenario}"}},
        )
        if queue:
            await queue.emit_tool_result(
                "偏好分析",
                f"完成 {len(result.get('preferences', []))} 位参与者分析",
                step="negotiation",
            )
            await queue.emit_tool_result(
                "冲突分析",
                f"发现 {len(result.get('conflicts', []))} 个冲突",
                step="negotiation",
            )
            await queue.emit_token(result.get("plan_text", ""), step="negotiation")
        return result
