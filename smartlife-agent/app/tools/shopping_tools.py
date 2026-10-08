"""购物只读工具契约，底层已迁移到真实 SQLite Repository。"""

from typing import Any, Dict, List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.retrieval.order_repository import OrderRepository
from app.retrieval.repositories import ProductRepository, ReviewRepository
from app.tools.tool_bundle import ToolBundle


class SearchProductsInput(BaseModel):
    query: str = Field(default="", description="搜索关键词或自然语言描述")
    category: Optional[str] = Field(default=None, description="商品分类")
    max_price: Optional[float] = Field(default=None, ge=0, description="最高价格")
    min_price: Optional[float] = Field(default=None, ge=0, description="最低价格")
    brand: Optional[str] = Field(default=None, description="品牌")


class GetProductReviewsInput(BaseModel):
    product_id: str = Field(description="商品ID")
    aspect: Optional[str] = Field(default=None, description="评价方面，如：质量、外观、性价比")


class GetOrderStatusInput(BaseModel):
    user_id: str = Field(description="当前用户ID；服务端强制按该用户过滤")
    order_id: Optional[str] = Field(default=None, description="订单ID，不填则查询所有订单")


class ShoppingToolProvider(ToolBundle):
    """商品、评价和只读订单工具提供方。"""

    def __init__(
        self,
        *,
        product_repository: Optional[ProductRepository] = None,
        review_repository: Optional[ReviewRepository] = None,
        order_repository: Optional[OrderRepository] = None,
    ):
        self.product_repository = product_repository or ProductRepository()
        self.review_repository = review_repository or ReviewRepository()
        self.order_repository = order_repository or OrderRepository()
        super().__init__(
            name="shopping-agent",
            description="购物服务：真实商品、评价和只读订单查询",
        )

    def _initialize_tools(self):
        product_repository = self.product_repository
        review_repository = self.review_repository
        order_repository = self.order_repository

        @tool("search_products", args_schema=SearchProductsInput)
        def search_products(
            query: str = "",
            category: Optional[str] = None,
            max_price: Optional[float] = None,
            min_price: Optional[float] = None,
            brand: Optional[str] = None,
        ) -> List[Dict[str, Any]]:
            """查询真实商品目录，支持关键词、分类、价格和品牌过滤。"""
            rows = product_repository.search(
                query,
                category=category,
                min_price=min_price,
                max_price=max_price,
                brand=brand,
            )
            return [{**row, "source": "products_db"} for row in rows]

        @tool("get_product_reviews", args_schema=GetProductReviewsInput)
        def get_product_reviews(
            product_id: str,
            aspect: Optional[str] = None,
        ) -> List[Dict[str, Any]]:
            """查询真实商品评价，可按评价方面进行关键词过滤。"""
            rows = review_repository.get_reviews(product_id, aspect=aspect)
            return [{**row, "source": "reviews_db"} for row in rows]

        @tool("get_order_status", args_schema=GetOrderStatusInput)
        def get_order_status(
            user_id: str,
            order_id: Optional[str] = None,
        ) -> List[Dict[str, Any]]:
            """按当前用户身份查询订单；不能查询其他用户的订单。"""
            rows = order_repository.get_order_status(user_id, order_id)
            return [{**row, "source": "orders_db"} for row in rows]

        self.tools = [search_products, get_product_reviews, get_order_status]

    def get_product_by_id(self, product_id: str) -> Optional[Dict[str, Any]]:
        rows = self.product_repository.search(str(product_id), limit=100)
        return next((row for row in rows if str(row.get("id")) == str(product_id)), None)
