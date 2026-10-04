"""
Agent 测试
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-key-for-testing")

def test_imports():
    from app.router import TaskRouter, TaskRoute
    from app.mcp_servers.base import BaseMCPServer
    from app.mcp_servers.shopping_server import ShoppingMCPServer
    from app.mcp_servers.travel_server import TravelMCPServer
    from app.mcp_servers.memory_server import MemoryMCPServer
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

def test_shopping_mcp_tools():
    from app.mcp_servers.shopping_server import ShoppingMCPServer
    tools = ShoppingMCPServer().get_tools()
    assert len(tools) == 4

def test_travel_mcp_tools():
    from app.mcp_servers.travel_server import TravelMCPServer
    tools = TravelMCPServer().get_tools()
    assert len(tools) == 4

def test_memory_mcp_tools():
    from app.mcp_servers.memory_server import MemoryMCPServer
    tools = MemoryMCPServer().get_tools()
    assert len(tools) == 5

def test_short_term_memory():
    from app.memory.short_term import ShortTermMemory
    mem = ShortTermMemory()
    mem.add_message("s1", "user", "你好")
    mem.add_message("s1", "assistant", "你好！")
    assert len(mem.get_history("s1")) == 2
    assert len(mem.get_langchain_messages("s1")) == 2

def test_weather_tool():
    from app.tools.weather import get_weather
    r = get_weather.invoke({"city": "杭州", "date": "2024-10-01"})
    assert "city" in r and "temperature" in r

def test_time_tool():
    from app.tools.time import get_current_time
    r = get_current_time.invoke({"timezone": "Asia/Shanghai"})
    assert "datetime" in r and "weekday" in r

def test_payment_tool():
    from app.tools.payment import create_payment
    r = create_payment.invoke({"order_id": "ORD-001", "amount": 499.0, "method": "alipay"})
    assert r["status"] == "success"

def test_map_tool():
    from app.tools.map import get_route
    r = get_route.invoke({"origin": "西湖", "destination": "灵隐寺", "mode": "driving"})
    assert "distance" in r and "duration" in r

def test_all_external_tools():
    from app.tools import get_all_tools
    assert len(get_all_tools()) >= 5

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
