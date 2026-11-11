"""商品匹配的单测：只用库里有的，替身要标，无货不当候选。

这一组的共同主题是"不许编"：库里没有就返回空，靠别名命中就写清楚是替代，
品牌偏好只能做软排序、不能把硬约束挤掉。
"""
from __future__ import annotations

from app.trip_gear.models import GearItem, TripFacts
from app.trip_gear.shopping import GearShopper


def _rows(*items):
    return [dict(item) for item in items]


class TestNoFabrication:
    def test_搜不到就留空(self):
        shopper = GearShopper(lambda **_: [])
        products = {}
        shopper.fill([GearItem(name="防潮垫", status="need_buy")], products)
        assert products == {}

    def test_无货不进入候选(self):
        # 旧实现是 `... if stock > 0] or rows`：全都没货时又把整表退回来，
        # 等于把无货商品当成推荐。这条钉住它。
        shopper = GearShopper(lambda **_: _rows({"name": "双人防水帐篷", "price": 599, "stock": 0}))
        products = {}
        shopper.fill([GearItem(name="帐篷", status="need_buy")], products)
        assert products == {}

    def test_有货的才被选中(self):
        rows = _rows(
            {"name": "清仓帐篷", "price": 199, "stock": 0},
            {"name": "双人防水帐篷", "price": 599, "stock": 6},
        )
        shopper = GearShopper(lambda **_: rows)
        products = {}
        shopper.fill([GearItem(name="帐篷", status="need_buy")], products)
        assert products["帐篷"]["price"] == 599


class TestMatchKind:
    def test_同名命中标为精确(self):
        shopper = GearShopper(lambda **_: _rows({"name": "四季保暖睡袋", "price": 299, "stock": 5}))
        products = {}
        shopper.fill([GearItem(name="睡袋", status="need_buy")], products)
        assert products["睡袋"]["match"]["kind"] == "exact"

    def test_不同名但同类命中的标为替代并写明检索词(self):
        shopper = GearShopper(lambda **_: _rows({"name": "轻薄羽绒外套", "price": 599, "stock": 5}))
        products = {}
        shopper.fill([GearItem(name="羽绒服", status="need_buy")], products)
        match = products["羽绒服"]["match"]
        assert match["kind"] == "substitute"
        assert match["query"] == "羽绒服"

    def test_预算不够时退到最便宜的那件(self):
        rows = _rows(
            {"name": "双人防水帐篷", "price": 599, "stock": 5},
            {"name": "单人帐篷", "price": 199, "stock": 5},
        )
        shopper = GearShopper(lambda **_: rows)
        products = {}
        shopper.fill([GearItem(name="帐篷", status="need_buy")], products, remaining_budget=250)
        assert products["帐篷"]["price"] == 199


class TestBrandPreference:
    def test_同档时偏好品牌胜出(self):
        rows = _rows(
            {"name": "双人防水帐篷", "brand": "其他", "price": 589, "stock": 5},
            {"name": "双人防水帐篷", "brand": "牧高笛", "price": 599, "stock": 5},
        )
        shopper = GearShopper(lambda **_: rows, preferred_brands=["牧高笛"])
        products = {}
        shopper.fill([GearItem(name="帐篷", status="need_buy")], products)
        assert products["帐篷"]["brand"] == "牧高笛"
        assert products["帐篷"]["match"]["by_preference"] is True

    def test_偏好不能为贵得离谱的款买单(self):
        rows = _rows(
            {"name": "双人防水帐篷", "brand": "其他", "price": 199, "stock": 5},
            {"name": "双人防水帐篷", "brand": "牧高笛", "price": 999, "stock": 5},
        )
        shopper = GearShopper(lambda **_: rows, preferred_brands=["牧高笛"])
        products = {}
        shopper.fill([GearItem(name="帐篷", status="need_buy")], products)
        assert products["帐篷"]["price"] == 199

    def test_没有偏好时按价格走(self):
        rows = _rows(
            {"name": "双人防水帐篷", "brand": "牧高笛", "price": 599, "stock": 5},
            {"name": "双人防水帐篷", "brand": "其他", "price": 589, "stock": 5},
        )
        shopper = GearShopper(lambda **_: rows)
        products = {}
        shopper.fill([GearItem(name="帐篷", status="need_buy")], products)
        assert products["帐篷"]["price"] == 589
        assert products["帐篷"]["match"]["by_preference"] is False


class TestRequestedItems:
    def test_user_requested_item_survives_a_tight_budget(self):
        by_query = {
            "女装": _rows({"name": "碎花雪纺连衣裙", "price": 299, "stock": 5}),
            "防晒霜": _rows({"name": "防晒霜SPF50+", "price": 89, "stock": 5}),
        }
        gear = [
            GearItem(name="女装", status="need_buy", user_requested=True),
            GearItem(name="防晒霜", status="need_buy"),
        ]
        products = {}

        GearShopper(lambda query, **_: by_query.get(query, []), facts=TripFacts()).fill(
            gear, products, remaining_budget=300
        )

        assert products.get("女装"), "点名的衣服不能被预算静默省掉"
        assert gear[1].status == "skipped"

    def test_requested_item_that_still_does_not_fit_stays_visible(self):
        by_query = {"女装": _rows({"name": "碎花雪纺连衣裙", "price": 299, "stock": 5})}
        gear = [GearItem(name="女装", status="need_buy", user_requested=True)]
        products = {}

        GearShopper(lambda query, **_: by_query.get(query, []), facts=TripFacts()).fill(
            gear, products, remaining_budget=100
        )

        assert gear[0].status == "need_buy"
        assert "预算不足" in gear[0].reason and "用户点名" in gear[0].reason

    def test_adult_trip_does_not_pick_kids_clothing(self):
        rows = _rows(
            {"name": "儿童T恤", "price": 59, "stock": 5, "category": "童装", "subcategory": "童装"},
            {"name": "纯棉圆领T恤", "price": 89, "stock": 5, "category": "服装", "subcategory": "男装"},
        )

        products = {}
        GearShopper(lambda **_: rows, facts=TripFacts()).fill(
            [GearItem(name="T恤", status="need_buy")], products
        )

        assert products["T恤"]["name"] == "纯棉圆领T恤"


def test_requested_one_piece_prefers_dress_when_budget_fits():
    """女生点名"一套衣服"：预算够连衣裙时，打分兜底不能退回半裙。"""
    rows = [
        {"name": "高腰A字半裙", "price": 229, "stock": 5, "category": "服装",
         "subcategory": "女装", "rating": 4.8},
        {"name": "碎花雪纺连衣裙", "price": 299, "stock": 5, "category": "服装",
         "subcategory": "女装", "rating": 4.6},
    ]
    gear = [GearItem(name="女装", status="need_buy", user_requested=True,
                    reason="用户点名：女生想买一套衣服")]
    products = {}

    GearShopper(lambda query, **_: rows, facts=TripFacts()).fill(
        gear, products, remaining_budget=700
    )

    assert products["女装"]["name"] == "碎花雪纺连衣裙"
