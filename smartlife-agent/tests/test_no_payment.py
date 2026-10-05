"""项目不提供支付或创建订单能力的回归测试。"""

from app.mcp_servers.shopping_server import ShoppingMCPServer
from app.tools import get_all_tools, get_safe_tools
from app.tools.payment import get_payment_tools


def test_payment_tools_are_disabled():
    assert get_payment_tools() == []
    assert not any(tool.name in {"create_payment", "get_payment_status"} for tool in get_all_tools())
    assert not any(tool.name in {"create_payment", "get_payment_status"} for tool in get_safe_tools())


def test_shopping_mcp_does_not_create_orders():
    names = {tool.name for tool in ShoppingMCPServer().get_tools()}
    assert "place_order" not in names
    assert names == {"search_products", "get_product_reviews", "get_order_status"}
