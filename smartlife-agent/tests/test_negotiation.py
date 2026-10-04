"""
社交协商测试
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-key-for-testing")

def test_preference_model():
    from app.negotiation.preference import UserPreference
    pref = UserPreference(user_id="user_001", priority_tags=["户外爱好者"], budget_constraint=3000)
    assert pref.user_id == "user_001"
    assert "户外爱好者" in pref.priority_tags

def test_common_ground():
    from app.negotiation.preference import PreferenceAnalyzer, UserPreference
    analyzer = PreferenceAnalyzer()
    prefs = [
        UserPreference(user_id="A", priority_tags=["户外", "美食", "拍照"], budget_constraint=2000),
        UserPreference(user_id="B", priority_tags=["美食", "购物"], budget_constraint=1500),
        UserPreference(user_id="C", priority_tags=["拍照", "美食", "文化"], budget_constraint=3000),
    ]
    result = analyzer.find_common_ground(prefs)
    assert "美食" in result["common_tags"]
    assert result["min_budget"] == 1500
    assert result["participant_count"] == 3

def test_conflict_detection():
    from app.negotiation.conflict import ConflictResolver
    resolver = ConflictResolver()
    prefs = [
        {"budget_constraint": 1000, "travel_preferences": {"preferred_styles": ["户外", "冒险"]}},
        {"budget_constraint": 5000, "travel_preferences": {"preferred_styles": ["购物", "美食"]}},
    ]
    conflicts = resolver.identify_conflicts(prefs)
    assert len(conflicts) > 0
    assert any(c["type"] == "budget" for c in conflicts)

def test_negotiation_graph_class():
    """测试协商图类定义（不实例化，避免API调用）"""
    from app.negotiation.graph import NegotiationGraph
    assert hasattr(NegotiationGraph, 'negotiate')
    assert hasattr(NegotiationGraph, '_build_graph')

def test_eval_test_cases_negotiation():
    from app.evaluation.test_cases import get_test_cases
    cases = get_test_cases("negotiation")
    assert len(cases) >= 1
    case = cases[0]
    assert "participants" in case
