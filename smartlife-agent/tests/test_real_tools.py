"""领域工具使用真实数据源的回归测试。

活动 provider 与地理编码都是外部服务，测试里一律注入假实现：
评测必须确定性、不联网（见 docs/architecture.md「评测」）。
真实链路的手工验证走 `scripts/verify_external_tools.py`。
"""

from app.tools.geo import Location
from app.tools.providers import ProviderResponse
from app.tools.shopping_tools import ShoppingToolProvider

HANGZHOU = Location(
    name="杭州",
    latitude=30.294,
    longitude=120.161,
    timezone="Asia/Shanghai",
    country="中国",
    country_code="CN",
    english="Hangzhou",
)


class _FakeActivitiesProvider:
    """假活动 provider：证明工具会消费 provider 结果，而不是读本地文件。"""

    name = "fake-activities"
    configured = True

    def fetch_with_meta(self, params, **kwargs):
        city = params.get("city", "")
        return ProviderResponse(
            payload={
                "city": city,
                "items": [
                    {"title": "西湖", "kind": "景点", "city": city},
                    {"title": "中国茶叶博物馆", "kind": "博物馆", "city": city},
                ],
                "source": "fake-activities",
                "kind": "poi",
                "note": "测试用假数据",
            },
            cache_status="miss",
            fetched_at=0.0,
            stale=False,
        )


class _EmptyActivitiesProvider(_FakeActivitiesProvider):
    """provider 拿不到任何条目（模拟外网不可用）。"""

    def fetch_with_meta(self, params, **kwargs):
        from app.tools.providers import ProviderUnavailable

        raise ProviderUnavailable("假装外网不可用")


def _patch_geo(monkeypatch):
    monkeypatch.setattr("app.tools.geo.resolve_location", lambda *a, **k: HANGZHOU)
    monkeypatch.setattr("app.tools.geo.resolve_city_english", lambda *a, **k: HANGZHOU.english)


def test_shopping_tools_use_real_repositories_instead_of_mock_rows():
    server = ShoppingToolProvider(actor_id="user_001")
    tools = {tool.name: tool for tool in server.get_tools()}

    products = tools["search_products"].invoke({
        "query": "跑步鞋",
        "max_price": 600,
    })
    reviews = tools["get_product_reviews"].invoke({
        "product_id": str(products[0]["id"]),
    })
    orders = tools["get_order_status"].invoke({})

    assert products
    assert all(row["price"] <= 600 for row in products)
    assert all(row["source"] == "products_db" for row in products)
    assert all(row["product_id"] == products[0]["id"] for row in reviews)
    assert all(row["user_id"] == "user_001" for row in orders)
    assert all(row["source"] == "orders_db" for row in orders)


def test_travel_tools_consume_activities_provider(monkeypatch):
    from app.tools.travel_tools import TravelToolProvider

    _patch_geo(monkeypatch)
    monkeypatch.setattr(
        "app.tools.providers.get_activities_provider", lambda: _FakeActivitiesProvider()
    )

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
    })
    assert any(item["city"] == "杭州" for item in destinations)
    assert destinations[0]["source"] == "local_destination_catalog"
    assert activities[0]["source"] == "fake-activities"
    assert activities[0]["available"] is True
    assert [item["title"] for item in activities[0]["activities"]] == ["西湖", "中国茶叶博物馆"]
    assert "search_hotels" not in tools


def test_activities_never_fall_back_to_another_city(monkeypatch):
    """回归：provider 不可用时不许拿 outdoor_activities.txt 冒充本地活动。"""
    from app.tools.travel_tools import TravelToolProvider

    _patch_geo(monkeypatch)
    monkeypatch.setattr(
        "app.tools.providers.get_activities_provider", lambda: _EmptyActivitiesProvider()
    )

    tool = TravelToolProvider().get_tool_by_name("get_local_activities")
    row = tool.invoke({"city": "新加坡", "date": "2026-11-20"})[0]

    assert row["available"] is False
    assert row["activities"] == []
    assert any("provider 不可用" in warning for warning in row["warnings"])


def test_local_activities_file_is_city_scoped(monkeypatch):
    """只有真存在 <city>_activities.txt 时才读本地文件。"""
    from app.tools.travel_tools import TravelToolProvider

    _patch_geo(monkeypatch)
    monkeypatch.setattr(
        "app.tools.providers.get_activities_provider", lambda: _EmptyActivitiesProvider()
    )

    tool = TravelToolProvider().get_tool_by_name("get_local_activities")
    hangzhou = tool.invoke({"city": "杭州", "date": "2026-11-20"})[0]
    other = tool.invoke({"city": "开普敦", "date": "2026-11-20"})[0]

    assert hangzhou["source"] == "local_activities_file"
    assert hangzhou["available"] is True
    assert other["available"] is False


def test_memory_tools_read_real_profile_and_require_approval_for_writes():
    from app.tools.memory_tools import MemoryToolProvider

    server = MemoryToolProvider(actor_id="user-missing")
    tools = {tool.name: tool for tool in server.get_tools()}

    profile = tools["get_user_profile"].invoke({})
    write = tools["save_preference"].invoke({
        "preference_type": "travel",
        "preference_data": {"likes": ["hiking"]},
    })

    assert profile["source"] == "md_memory"
    assert write["status"] == "approval_required"
    assert write["interrupt"]


def test_current_registry_excludes_hotel_tool():
    from app.tools import get_all_tools, get_safe_tools

    safe_names = [tool.name for tool in get_safe_tools(actor_id="user_001")]
    all_names = [tool.name for tool in get_all_tools(actor_id="user_001")]

    assert {"search_products", "get_product_reviews", "get_order_status"} <= set(safe_names)
    assert {"search_destinations", "get_local_activities"} <= set(safe_names)
    assert "search_hotels" not in safe_names
    assert "search_hotels" not in all_names
    assert "save_preference" not in safe_names
    assert "save_event" not in safe_names
    assert "save_preference" in all_names
    assert "place_order" not in all_names
