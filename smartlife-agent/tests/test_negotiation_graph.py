"""协商图结构化方案和异步事件测试。"""

import asyncio

from app.negotiation.graph import NegotiationGraph
from app.negotiation.preference import UserPreference


class _Analyzer:
    def analyze(self, user_id, user_info, purchase_history=None, travel_history=None):
        return UserPreference(
            user_id=user_id,
            travel_preferences=user_info.get("travel_preferences", {}),
            budget_constraint=user_info.get("budget_constraint", 0),
            priority_tags=user_info.get("priority_tags", []),
        )

    def find_common_ground(self, preferences):
        tags = sorted({tag for item in preferences for tag in item.priority_tags})
        budgets = [item.budget_constraint for item in preferences if item.budget_constraint > 0]
        return {
            "common_tags": tags,
            "min_budget": min(budgets) if budgets else 0,
            "participant_count": len(preferences),
            "tag_distribution": {},
        }


class _Resolver:
    def __init__(self):
        self.resolve_calls = 0

    def identify_conflicts(self, preferences):
        budgets = [item.get("budget_constraint", 0) for item in preferences]
        if len(budgets) > 1 and max(budgets) - min(budgets) > 500:
            return [{"type": "budget", "description": "预算差异", "severity": "high"}]
        return []

    def resolve(self, preferences, conflicts, common_ground):
        self.resolve_calls += 1
        return {"compromise_plan": "按最低预算执行，差异项投票"}


class _Queue:
    def __init__(self):
        self.results = []
        self.tokens = []

    async def emit_tool_result(self, name, output, step=""):
        self.results.append((name, output, step))

    async def emit_token(self, token, step=""):
        self.tokens.append((token, step))


def _participants():
    return [
        {
            "user_id": "a",
            "budget_constraint": 1200,
            "priority_tags": ["美食"],
            "travel_preferences": {"preferred_styles": ["美食"]},
        },
        {
            "user_id": "b",
            "budget_constraint": 1100,
            "priority_tags": ["美食"],
            "travel_preferences": {"preferred_styles": ["文化"]},
        },
    ]


def test_negotiation_graph_generates_structured_plan_without_conflicts():
    graph = NegotiationGraph(_Analyzer(), _Resolver())

    result = graph.negotiate(_participants(), scenario="travel")

    assert result["status"] == "completed"
    assert result["plan"]["participant_count"] == 2
    assert result["plan"]["common_tags"] == ["美食"]
    assert result["plan"]["requires_confirmation"] is False
    assert "协商方案" in result["plan_text"]


def test_negotiation_graph_routes_budget_conflict_to_compromise():
    resolver = _Resolver()
    graph = NegotiationGraph(_Analyzer(), resolver)
    participants = _participants()
    participants[1]["budget_constraint"] = 5000

    result = graph.negotiate(participants, scenario="travel")

    assert resolver.resolve_calls == 1
    assert result["plan"]["requires_confirmation"] is True
    assert "投票确认" in result["plan_text"]


def test_negotiation_graph_run_streaming_emits_results_and_plan_text():
    graph = NegotiationGraph(_Analyzer(), _Resolver())
    queue = _Queue()

    result = asyncio.run(graph.run_streaming(_participants(), "travel", queue))

    assert result["plan_text"]
    assert [item[0] for item in queue.results] == ["偏好分析", "冲突分析"]
    assert queue.tokens == [(result["plan_text"], "negotiation")]
