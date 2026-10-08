"""
Agent 测试
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key-for-testing")

def test_imports():
    from app.router import TaskRouter, TaskRoute
    from app.tools.tool_bundle import ToolBundle
    from app.tools.shopping_tools import ShoppingToolProvider
    from app.tools.travel_tools import TravelToolProvider
    from app.tools.memory_tools import MemoryToolProvider
    from app.agents.reflection import ReflectionAgent, ReflectionResult
    from app.agents.shopping_agent import ShoppingAgent
    from app.agents.travel_agent import TravelAgent
    from app.agents.negotiation_agent import NegotiationAgent
    from app.retrieval.nl2sql import NL2SQLChain, SQLQuery
    from app.retrieval.rag import RAGRetriever
    from app.retrieval.reranker import CrossEncoderReranker
    from app.retrieval.fusion import HybridRetriever
    from app.memory.short_term import ShortTermMemory
    from app.memory.long_term import LongTermMemory
    from app.memory.compressor import MemoryCompressor
    from app.negotiation.preference import PreferenceAnalyzer, UserPreference
    from app.negotiation.conflict import ConflictResolver
    from app.negotiation.graph import NegotiationGraph
    from app.evaluation.ragas_eval import RAGASEvaluator, EvalResult
    from app.evaluation.test_cases import TEST_CASES, get_test_cases
    from app.tools import get_all_tools

def test_task_route_model():
    from app.router import TaskRoute
    route = TaskRoute(route="react", reason="简单", complexity="simple", estimated_steps=1)
    assert route.route == "react"

def test_shopping_tools():
    from app.tools.shopping_tools import ShoppingToolProvider
    tools = ShoppingToolProvider().get_tools()
    assert len(tools) == 3

def test_travel_tools():
    from app.tools.travel_tools import TravelToolProvider
    tools = TravelToolProvider().get_tools()
    assert len(tools) == 3

def test_memory_tools():
    from app.tools.memory_tools import MemoryToolProvider
    tools = MemoryToolProvider().get_tools()
    assert len(tools) == 5

def test_short_term_memory():
    from app.memory.short_term import ShortTermMemory
    mem = ShortTermMemory()
    mem.add_message("s1", "user", "你好")
    mem.add_message("s1", "assistant", "你好！")
    assert len(mem.get_history("s1")) == 2
    assert len(mem.get_langchain_messages("s1")) == 2

def test_weather_tool(monkeypatch):
    from datetime import date

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"temperature": "21°C", "condition": "晴", "humidity": "50%"}

    monkeypatch.setenv("SMARTLIFE_WEATHER_API_URL", "https://weather.test/query")
    monkeypatch.setattr("app.tools.providers.requests.get", lambda *args, **kwargs: _Response())
    from app.tools.weather import get_weather
    r = get_weather.invoke({"city": "杭州", "date": date.today().isoformat()})
    assert "city" in r and "temperature" in r

def test_time_tool():
    from app.tools.time import get_current_time
    r = get_current_time.invoke({"timezone": "Asia/Shanghai"})
    assert "datetime" in r and "weekday" in r

def test_map_tool(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"distance": "3.0公里", "duration": "10分钟", "route": "真实路线"}

    monkeypatch.setenv("SMARTLIFE_ROUTE_API_URL", "https://route.test/query")
    monkeypatch.setattr("app.tools.providers.requests.get", lambda *args, **kwargs: _Response())
    from app.tools.map import get_route
    r = get_route.invoke({"origin": "西湖", "destination": "灵隐寺", "mode": "driving"})
    assert "distance" in r and "duration" in r

def test_all_external_tools():
    from app.tools import get_all_tools
    assert len(get_all_tools()) >= 4

def test_preference_model():
    from app.negotiation.preference import UserPreference
    assert UserPreference(user_id="t", priority_tags=["户外"]).user_id == "t"

def test_nl2sql_model():
    from app.retrieval.nl2sql import SQLQuery
    q = SQLQuery(sql="SELECT * FROM products", explanation="test", needs_rag=[])
    assert q.sql.startswith("SELECT")

def test_test_cases():
    from app.evaluation.test_cases import get_test_cases
    assert len(get_test_cases("shopping")) >= 5

def test_reflection_result():
    from app.agents.reflection import ReflectionResult
    r = ReflectionResult(is_satisfactory=False, issues=["问题"], suggestions=["建议"])
    assert not r.is_satisfactory

def test_ragas_evaluator():
    from app.evaluation.ragas_eval import RAGASEvaluator
    ev = RAGASEvaluator()
    ev.add_test_case("防水鞋", "GORE-TEX", "GORE-TEX防水", "推荐GORE-TEX防水鞋")
    result = ev.evaluate_all()
    assert result.faithfulness >= 0
