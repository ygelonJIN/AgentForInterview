"""项目不提供支付或创建订单能力的回归测试。

支付工具已整体移除（模块、导出、shim 全删），这里只保留"能力边界"回归：
任何注册进工具面的工具都不许出现下单/支付名。
"""

from app.tools.registry import get_registry
from app.tools.shopping_tools import ShoppingToolProvider

FORBIDDEN = {
    "create_payment",
    "get_payment_status",
    "place_order",
    "pay",
    "refund",
}


def test_no_payment_or_order_tools_anywhere():
    names = {tool.name for tool in get_registry().tools()}
    assert not (names & FORBIDDEN), f"支付/下单工具不应存在: {names & FORBIDDEN}"


def test_shopping_mcp_does_not_create_orders():
    names = {tool.name for tool in ShoppingToolProvider().get_tools()}
    assert "place_order" not in names
    assert names == {"search_products", "get_product_reviews", "get_order_status"}
