"""
基础测试文件
"""

import pytest
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

def test_imports():
    """测试导入"""
    try:
        from app.router import TaskRouter, TaskRoute
        from app.tools.tool_bundle import ToolBundle
        from app.tools.shopping_tools import ShoppingToolProvider
        from app.tools.travel_tools import TravelToolProvider
        from app.tools.memory_tools import MemoryToolProvider
        assert True
    except ImportError as e:
        pytest.fail(f"导入失败: {e}")

def test_task_route_model():
    """测试TaskRoute模型"""
    from app.router import TaskRoute
    
    # 测试有效数据
    route = TaskRoute(
        route="react",
        reason="简单查询任务",
        complexity="simple",
        estimated_steps=1
    )
    assert route.route == "react"
    assert route.reason == "简单查询任务"
    assert route.complexity == "simple"
    assert route.estimated_steps == 1

def test_shopping_tool_provider():
    """测试 ShoppingToolProvider"""
    from app.tools.shopping_tools import ShoppingToolProvider
    
    server = ShoppingToolProvider()
    tools = server.get_tools()
    
    assert len(tools) == 3
    tool_names = [tool.name for tool in tools]
    assert "search_products" in tool_names
    assert "get_product_reviews" in tool_names
    assert "get_order_status" in tool_names

def test_travel_tool_provider():
    """测试 TravelToolProvider"""
    from app.tools.travel_tools import TravelToolProvider
    
    server = TravelToolProvider()
    tools = server.get_tools()
    
    assert len(tools) == 3
    tool_names = [tool.name for tool in tools]
    assert "search_destinations" in tool_names
    assert "get_local_activities" in tool_names
    assert "plan_itinerary" in tool_names

def test_memory_tool_provider():
    """测试 MemoryToolProvider"""
    from app.tools.memory_tools import MemoryToolProvider
    
    server = MemoryToolProvider()
    tools = server.get_tools()
    
    assert len(tools) == 5
    tool_names = [tool.name for tool in tools]
    assert "get_user_profile" in tool_names
    assert "save_preference" in tool_names
    assert "get_cross_scene_memories" in tool_names
    assert "save_event" in tool_names
    assert "get_user_context" in tool_names

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
