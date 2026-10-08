"""领域工具使用真实数据源的回归测试。"""

from app.tools.shopping_tools import ShoppingToolProvider


def test_shopping_tools_use_real_repositories_instead_of_mock_rows():
    server = ShoppingToolProvider()
    tools = {tool.name: tool for tool in server.get_tools()}

    products = tools["search_products"].invoke({
        "query": "跑步鞋",
        "max_price": 600,
    })
    reviews = tools["get_product_reviews"].invoke({
        "product_id": str(products[0]["id"]),
    })
    orders = tools["get_order_status"].invoke({
        "user_id": "user_001",
    })

    assert products
    assert all(row["price"] <= 600 for row in products)
    assert all(row["source"] == "products_db" for row in products)
    assert all(row["product_id"] == products[0]["id"] for row in reviews)
    assert all(row["user_id"] == "user_001" for row in orders)
    assert all(row["source"] == "orders_db" for row in orders)


def test_travel_tools_use_local_catalog_without_hotel_tool():
    from app.tools.travel_tools import TravelToolProvider

    server = TravelToolProvider()
    tools = {tool.name: tool for tool in server.get_tools()}

    destinations = tools["search_destinations"].invoke({
        "preference": "文化 美食",
        "budget": 3000,
        "days": 3,
    })
    activities = tools["get_local_activities"].invoke({
        "city": "杭州",
        "date": "2026-10-10",
        "interests": ["骑行"],
    })
    plan = tools["plan_itinerary"].invoke({
        "destination": "杭州",
        "days": 2,
        "budget": 1200,
        "interests": ["骑行"],
    })

    assert any(item["city"] == "杭州" for item in destinations)
    assert destinations[0]["source"] == "local_destination_catalog"
    assert activities[0]["source"] == "local_activities_file"
    assert activities[0]["activities"]
    assert plan["source"] == "local_activity_planner"
    assert len(plan["itinerary"]) == 2
    assert "search_hotels" not in tools


def test_memory_tools_read_real_profile_and_require_approval_for_writes():
    from app.tools.memory_tools import MemoryToolProvider

    server = MemoryToolProvider()
    tools = {tool.name: tool for tool in server.get_tools()}

    profile = tools["get_user_profile"].invoke({"user_id": "user-missing"})
    write = tools["save_preference"].invoke({
        "user_id": "user-missing",
        "preference_type": "travel",
        "preference_data": {"likes": ["hiking"]},
    })

    assert profile["source"] == "md_memory"
    assert write["status"] == "approval_required"
    assert write["interrupt"]


def test_current_registry_excludes_hotel_tool():
    from app.tools import get_all_tools, get_safe_tools

    safe_names = [tool.name for tool in get_safe_tools()]
    all_names = [tool.name for tool in get_all_tools()]

    assert {"search_products", "get_product_reviews", "get_order_status"} <= set(safe_names)
    assert {"search_destinations", "get_local_activities", "plan_itinerary"} <= set(safe_names)
    assert "search_hotels" not in safe_names
    assert "search_hotels" not in all_names
    assert "save_preference" not in safe_names
    assert "save_event" not in safe_names
    assert "save_preference" in all_names
    assert "place_order" not in all_names
