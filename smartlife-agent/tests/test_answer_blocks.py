"""回答块构造测试：字段只能来自真实数据，空块必须被丢掉。"""

from app.ui.blocks import (
    clean,
    lead_block,
    order_blocks,
    shopping_blocks,
    text_block,
    text_only,
)


def _types(blocks):
    return [block["type"] for block in blocks]


def test_shopping_blocks_use_only_library_rows():
    retrieval = {
        "products": [
            {"id": "P1", "name": "牧高笛 冷山 2 人帐", "price": 599, "rating": 4.7, "stock": 12, "brand": "牧高笛", "category": "帐篷"},
            {"id": "P2", "name": "挪客 信封棉睡袋", "price": 189, "rating": 4.5, "stock": 34, "brand": "挪客", "category": "睡袋"},
        ]
    }

    blocks = shopping_blocks(retrieval, "两件都合适。", query="露营")

    assert _types(blocks) == ["lead", "stats", "cards", "table", "text"]
    stats = {item["label"]: item["value"] for item in blocks[1]["items"]}
    assert stats["命中"] == "2 件"
    assert stats["价格区间"] == "¥189–¥599"
    assert stats["平均评分"] == "4.6"
    assert blocks[2]["items"][0]["name"] == "牧高笛 冷山 2 人帐"
    assert blocks[3]["rows"][-1][-1] == "¥788"  # 合计由真实价格算出
    assert blocks[-1]["text"] == "两件都合适。"


def test_shopping_without_products_falls_back_to_text():
    blocks = shopping_blocks({}, "没有匹配的商品。", query="限量款")

    assert _types(blocks) == ["text"]


def test_order_blocks_sum_real_amounts():
    orders = [
        {"id": "A1", "product_name": "帐篷", "status": "已发货", "total_price": 399, "created_at": "2026-11-10"},
        {"id": "A2", "product_name": "睡袋", "status": "已完成", "total_price": 189, "created_at": "2026-11-01"},
    ]

    blocks = order_blocks(orders, "两笔都在路上。")

    assert _types(blocks) == ["lead", "stats", "timeline", "text"]
    stats = {item["label"]: item["value"] for item in blocks[1]["items"]}
    assert stats["订单数"] == "2 笔"
    assert stats["金额合计"] == "¥588"
    assert len(blocks[2]["items"]) == 2


def test_empty_blocks_are_dropped():
    assert clean([None, text_block("  "), lead_block(""), {"type": "cards", "items": []}]) == []
    assert text_only("") == []


def test_block_payload_survives_json_round_trip():
    blocks = shopping_blocks(
        {"products": [{"id": "P1", "name": "帐篷", "price": 100}]},
        "正文",
    )
    import json

    assert json.loads(json.dumps(blocks, ensure_ascii=False)) == blocks
