"""装备清单只由商品库决定：库里没有的不进清单，用户已有的必须记下来。"""

import asyncio

from app.trip_gear.constraints import drop_owned_duplicates
from app.trip_gear.gear import derive_gear
from app.trip_gear.itinerary import fallback_itinerary
from app.trip_gear.models import Constraint, GearItem, TripFacts
from app.trip_gear.owned import apply_owned, extract_owned
from app.trip_gear.render import render
from app.trip_gear.shopping import GearShopper
from app.ui.theme import _gear_columns, _gear_counts


def _formal_gear():
    facts = TripFacts(destination="上海", days=2, people=2, transport="train")
    itinerary = fallback_itinerary(facts)
    return facts, itinerary, derive_gear(facts, itinerary, extra_text="见客户")


def test_suit_jacket_matches_formal_gear_without_any_alias_table():
    """"西装外套"能对上"西装"，靠的是词形包含，不是"西装外套→正装"这种映射表。"""
    _facts, _itinerary, gear = _formal_gear()
    assert any(item.name == "西装" for item in gear)

    gear = apply_owned(gear, extract_owned("我有一件西装外套"))

    formal = next(item for item in gear if item.name == "西装")
    assert formal.status == "owned"
    assert "已有" in formal.reason


def test_owned_item_not_derived_by_rules_is_still_recorded():
    facts = TripFacts(destination="上海", days=2, people=1)
    itinerary = fallback_itinerary(facts)
    gear = derive_gear(facts, itinerary)  # 没有正式场合，不会派生"西装"

    gear = apply_owned(gear, extract_owned("我有一件西装外套"))

    appended = [item for item in gear if item.name == "西装外套"]
    assert appended and appended[0].status == "owned"


def test_every_explicitly_owned_item_is_recorded():
    """用户说了就记下来：不再靠一张"哪些品类值得回填"的白名单。"""
    facts = TripFacts(destination="上海", days=2, people=1)
    itinerary = fallback_itinerary(facts)
    gear = derive_gear(facts, itinerary)

    gear = apply_owned(gear, extract_owned("我有一支口红"))

    assert any(item.name == "口红" and item.status == "owned" for item in gear)


def test_shopper_removes_items_the_catalog_does_not_have():
    gear = [
        GearItem(name="帐篷", status="need_buy", reason="露营过夜"),
        GearItem(name="帽子", status="need_buy", reason="户外日晒"),
    ]
    shopper = GearShopper(lambda **_: [])
    products = {}

    shopper.fill(gear, products)

    assert gear == []          # 库里没有的直接从清单里移除
    assert products == {}


def test_shopper_keeps_affordable_items_and_marks_budget_blocked():
    rows = [{"name": "双人帐篷", "price": 599, "stock": 5, "id": 1}]
    gear = [GearItem(name="帐篷", status="need_buy", reason="露营过夜", qty=2)]
    products = {}

    GearShopper(lambda **_: rows).fill(gear, products, remaining_budget=100)

    assert gear and "预算不足，未选款" in gear[0].reason
    assert products == {}


def test_render_only_lists_catalog_items():
    facts = TripFacts(destination="上海", days=3, people=2, transport="train")
    itinerary = fallback_itinerary(facts)
    gear = derive_gear(facts, itinerary, extra_text="见客户")
    gear = apply_owned(gear, extract_owned("我有一件西装外套"))
    products = {"皮鞋": {"name": "某某 皮鞋", "brand": "某某", "price": 399}}
    # 真实链路里 shop 已经把没商品的项移除了，这里模拟同样的状态
    gear = [item for item in gear if item.status != "need_buy" or item.name in products]

    text = render(facts, [], itinerary, gear, products, [])

    assert "皮鞋" in text
    assert "另有" not in text      # 不再报"库里没有几项"
    assert "自备" not in text       # 不再有自备分区
    assert "未匹配" not in text


def test_render_still_reports_budget_blocked_items():
    facts = TripFacts(destination="三亚", days=2, people=1, budget=200)
    itinerary = fallback_itinerary(facts)
    gear = [GearItem(name="泳衣", qty=1, status="need_buy", reason="水上活动；预算不足，未选款")]

    text = render(facts, [], itinerary, gear, {}, [])

    assert "预算不足，未选款" in text
    assert "泳衣" in text


def test_panel_has_two_groups_and_no_placeholder():
    gear = [
        {"name": "西装", "qty": 1, "status": "owned", "reason": "已有（对话）"},
        {"name": "皮鞋", "qty": 1, "status": "need_buy", "reason": "行程含正式场合"},
        # 即使有历史数据里的 self_provided，也不再展示
        {"name": "身份证", "qty": 1, "status": "self_provided", "reason": "通用"},
    ]
    products = {"皮鞋": {"name": "某某 皮鞋", "brand": "某某", "price": 399}}

    html, foot = _gear_columns(gear, products)

    # 两组横排，不再固定三列：条目少的时候不留空列。
    assert html.count("sl-gear-line") == 2
    assert "身份证" not in html and "身份证" not in foot
    assert "另有" not in foot
    assert _gear_counts(gear) == (1, 1)


def test_model_inferred_owned_constraint_is_dropped():
    constraints = [
        Constraint(
            text="装备准备时把用户已有的西装外套纳入考虑，优先使用现有外套。",
            source="模型推断",
            hard=False,
        ),
        Constraint(text="行程要宽松，不赶路", source="模型推断", hard=False),
    ]

    kept = drop_owned_duplicates(constraints, ["西装外套"])

    assert [item.text for item in kept] == ["行程要宽松，不赶路"]


def test_graph_records_suit_jacket_as_owned_and_drops_duplicate_constraint():
    from app.graphs.context import TurnContext
    from app.graphs.subgraphs.trip_gear import build_trip_gear_graph

    class Services:
        def get_chat_history(self, thread_id):
            return []

    async def planner(facts, constraints):
        from app.trip_gear.itinerary import fallback_itinerary

        return fallback_itinerary(facts)

    async def infer(message, facts, constraints):
        return [{
            "text": "装备准备时把用户已有的西装外套纳入考虑，优先使用现有外套。",
            "key": "other",
            "hard": False,
        }]

    def search(query="", limit=5):
        if "皮鞋" in query:
            return [{
                "id": 1, "name": "木林森 正装皮鞋 黑色", "brand": "木林森", "price": 299,
                "stock": 10, "subcategory": "日用", "rating": 4.6, "description": "商务皮鞋",
            }]
        return []

    ctx = TurnContext(services=Services(), trace_id="owned").with_extra(
        planner=planner,
        product_search=search,
        constraint_infer=infer,
    )
    result = asyncio.run(build_trip_gear_graph().ainvoke(
        {
            "user_message": "下周三去上海出差两天，我有一件西装外套，帮我看看带什么",
            "user_id": "u1",
            "thread_id": "t1",
        },
        context=ctx,
    ))

    owned = [item for item in result["gear"] if item["status"] == "owned"]
    assert any(item["name"] in {"西装", "西装外套"} for item in owned)
    assert not any(
        "西装外套" in item["text"] and item["source"] == "模型推断"
        for item in result["constraints"]
    )
    # 清单里每条待购都必须有商品
    for item in result["gear"]:
        if item["status"] == "need_buy":
            assert item["name"] in result["products"]
