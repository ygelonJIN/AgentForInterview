"""模型挑款：一次调用挑完、只认候选里的 id、超预算和重复都拒绝。"""

import asyncio

import pytest

from app.trip_gear import select_by_model
from app.trip_gear.models import GearItem
from app.trip_gear.select import rank


def _candidates(*rows):
    return [dict(row) for row in rows]


def _items():
    return [
        GearItem(name="睡袋", qty=2, reason="露营过夜", status="need_buy"),
        GearItem(name="帐篷", qty=1, reason="露营过夜", status="need_buy"),
    ]


def _candidates_for(item):
    if item.name == "睡袋":
        return _candidates(
            {"id": 1, "name": "羽绒睡袋 -10℃", "price": 800, "stock": 5, "rating": 4.8},
            {"id": 2, "name": "棉睡袋 5℃", "price": 200, "stock": 5, "rating": 4.2},
        )
    return _candidates(
        {"id": 3, "name": "双人帐篷", "price": 600, "stock": 5, "rating": 4.7},
        {"id": 4, "name": "单人帐篷", "price": 300, "stock": 5, "rating": 4.4},
    )


class _Choice:
    def __init__(self, gear, product_id, reason=""):
        self.gear = gear
        self.product_id = product_id
        self.reason = reason


class _Result:
    def __init__(self, choices):
        self.choices = choices


def test_switch_defaults_on_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv(select_by_model.SWITCH_ENV, raising=False)
    assert select_by_model.is_enabled() is True
    monkeypatch.setenv(select_by_model.SWITCH_ENV, "0")
    assert select_by_model.is_enabled() is False


def test_build_entries_skips_items_without_a_choice():
    items = [
        GearItem(name="睡袋", qty=1, status="need_buy"),
        GearItem(name="雨伞", qty=1, status="need_buy"),
    ]

    def candidates_for(item):
        if item.name == "睡袋":
            return _candidates(
                {"id": 1, "name": "睡袋A", "price": 100, "stock": 5},
                {"id": 2, "name": "睡袋B", "price": 200, "stock": 5},
            )
        return _candidates({"id": 9, "name": "雨伞", "price": 50, "stock": 5})

    entries = select_by_model.build_entries(items, candidates_for, rank)

    assert [entry["gear"] for entry in entries] == ["睡袋"]


def test_validation_rejects_unknown_duplicate_and_over_budget():
    entries = [
        {"gear": "睡袋", "qty": 2, "reason": "", "candidates": [
            {"id": 1, "price": 800}, {"id": 2, "price": 200},
        ]},
        {"gear": "帐篷", "qty": 1, "reason": "", "candidates": [
            {"id": 3, "price": 600}, {"id": 4, "price": 300},
        ]},
    ]

    accepted = select_by_model._validate(
        [
            _Choice("睡袋", 99),        # 不在候选里
            _Choice("帐篷", 4),         # 合法
            _Choice("帐篷", 3),         # 同一商品不能重复
        ],
        entries,
        remaining=1000,
    )
    assert list(accepted) == ["帐篷"]

    # 睡袋 ×2 单价 800 = 1600，超预算
    assert select_by_model._validate([_Choice("睡袋", 1)], entries, remaining=1000) == {}


def test_choose_products_calls_model_once_and_caches(monkeypatch):
    select_by_model.clear_cache()
    monkeypatch.delenv(select_by_model.SWITCH_ENV, raising=False)
    calls = {"n": 0}

    async def fake_ainvoke(_llm, _messages, _schema, _variables):
        calls["n"] += 1
        return _Result([_Choice("睡袋", 2), _Choice("帐篷", 3)])

    monkeypatch.setattr(select_by_model, "ainvoke_json", fake_ainvoke)

    entries = select_by_model.build_entries(_items(), _candidates_for, rank)
    first = asyncio.run(select_by_model.choose_products(entries, remaining=2000))
    second = asyncio.run(select_by_model.choose_products(entries, remaining=2000))

    assert first["睡袋"]["id"] == 2 and first["帐篷"]["id"] == 3
    assert second == first
    assert calls["n"] == 1, "同一组候选应该走缓存"


def test_choose_products_returns_empty_when_disabled(monkeypatch):
    select_by_model.clear_cache()
    monkeypatch.setenv(select_by_model.SWITCH_ENV, "0")

    entries = select_by_model.build_entries(_items(), _candidates_for, rank)
    assert asyncio.run(select_by_model.choose_products(entries, remaining=2000)) == {}


def test_choose_products_swallows_model_failure(monkeypatch):
    select_by_model.clear_cache()
    monkeypatch.delenv(select_by_model.SWITCH_ENV, raising=False)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(select_by_model, "ainvoke_json", boom)

    entries = select_by_model.build_entries(_items(), _candidates_for, rank)
    assert asyncio.run(select_by_model.choose_products(entries, remaining=2000)) == {}


def test_shop_node_uses_model_choice_and_keeps_budget():
    from app.graphs.context import TurnContext
    from app.graphs.subgraphs.trip_gear import build_trip_gear_graph

    class Services:
        def get_chat_history(self, thread_id):
            return []

    async def planner(facts, constraints):
        from app.trip_gear.itinerary import fallback_itinerary

        return fallback_itinerary(facts)

    def search(query="", limit=5):
        rows = {
            "睡袋": [
                {"id": 1, "name": "羽绒睡袋 -10℃", "brand": "甲", "price": 800, "stock": 5,
                 "subcategory": "露营", "rating": 4.8, "description": "睡袋"},
                {"id": 2, "name": "棉睡袋 5℃", "brand": "乙", "price": 200, "stock": 5,
                 "subcategory": "露营", "rating": 4.2, "description": "睡袋"},
            ],
            "帐篷": [
                {"id": 3, "name": "双人帐篷", "brand": "丙", "price": 600, "stock": 5,
                 "subcategory": "露营", "rating": 4.7, "description": "帐篷"},
                {"id": 4, "name": "单人帐篷", "brand": "丁", "price": 300, "stock": 5,
                 "subcategory": "露营", "rating": 4.4, "description": "帐篷"},
            ],
        }
        for key, value in rows.items():
            if key in query:
                return value[:limit]
        return []

    seen = {}

    async def product_select(entries, facts, remaining):
        seen["executed"] = True
        return {
            entry["gear"]: entry["candidates"][0]
            for entry in entries
        }

    ctx = TurnContext(services=Services(), trace_id="sel").with_extra(
        planner=planner,
        product_search=search,
        product_select=product_select,
    )
    result = asyncio.run(build_trip_gear_graph().ainvoke(
        {"user_message": "周末两个人去莫干山露营，预算3000", "user_id": "u1", "thread_id": "t"},
        context=ctx,
    ))

    assert seen.get("executed") is True
    products = result["products"]
    assert products["睡袋"]["id"] == 1
    assert products["睡袋"]["match"]["by"] == "model"
    # 帐篷也应该被模型选掉，不会再被打分覆盖
    assert products["帐篷"]["id"] == 3
