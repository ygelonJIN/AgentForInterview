"""装备派生与已有判定。"""

from app.trip_gear.gear import derive_gear
from app.trip_gear.itinerary import fallback_itinerary
from app.trip_gear.models import DayPlan, Itinerary, ItineraryItem, TripFacts
from app.trip_gear.owned import apply_owned, extract_from_memories, extract_owned


def _itinerary(*activities: str, days: int = 2) -> Itinerary:
    plans = []
    for day in range(1, days + 1):
        plans.append(
            DayPlan(
                day=day,
                items=[
                    ItineraryItem(time="10:00", activity=activity, duration_min=90, cost=0)
                    for activity in activities
                ],
            )
        )
    return Itinerary(destination="莫干山", days=plans)


def _by_name(gear):
    return {item.name: item for item in gear}


def test_camping_rules_cover_tent_bag_pad_and_lamp():
    facts = TripFacts(destination="莫干山", days=2, people=2, lodging="camp")
    gear = _by_name(derive_gear(facts, fallback_itinerary(facts)))

    assert gear["帐篷"].qty == 1
    assert gear["睡袋"].qty == 2
    assert gear["防潮垫"].qty == 2
    assert gear["头灯"].qty == 2
    assert "身份证" not in gear  # 只派生商品词，不再有"自备"清单


def test_rain_and_formal_occasion_add_gear():
    facts = TripFacts(
        destination="上海",
        days=2,
        people=1,
        rain_probability=60,
        min_temp_c=12,
    )
    gear = _by_name(derive_gear(facts, _itinerary("见客户", "晚餐")))

    assert "西装" in gear and "皮鞋" in gear
    assert "雨伞" in gear and "登山鞋" in gear
    assert "羽绒服" in gear


def test_formal_occasion_is_kept_even_when_itinerary_wording_is_generic():
    facts = TripFacts(destination="上海", days=2, people=1)
    gear = _by_name(
        derive_gear(facts, _itinerary("自由活动"), extra_text="明天见客户")
    )

    assert "西装" in gear and "皮鞋" in gear


def test_long_trip_derives_clothing_quantity():
    facts = TripFacts(destination="成都", days=4, people=1)
    gear = _by_name(derive_gear(facts, _itinerary("逛博物馆", days=4)))

    assert gear["T恤"].qty == 3
    assert gear["洗衣袋"].qty == 1


def test_owned_items_mark_status_and_leave_only_the_gap():
    facts = TripFacts(destination="莫干山", days=2, people=2, lodging="camp")
    gear = derive_gear(facts, fallback_itinerary(facts))
    owned = extract_owned("我有帐篷和睡袋")

    apply_owned(gear, owned)
    items = _by_name(gear)

    assert items["帐篷"].status == "owned"
    assert items["睡袋"].status == "need_buy"
    assert items["睡袋"].qty == 1
    assert "还差 1 件" in items["睡袋"].reason


def test_wish_to_buy_is_not_treated_as_owned():
    assert extract_owned("我想买帐篷和睡袋") == []
    assert extract_owned("需要买一个防潮垫") == []


def test_memory_ownership_keeps_its_source():
    memories = [
        {
            "content": "用户上周买了帐篷和登山杖",
            "metadata": {"memory_id": "abcd12345678"},
        }
    ]
    owned = extract_from_memories(memories)

    names = {item.name: item for item in owned}
    assert set(names) == {"帐篷", "登山杖"}
    assert names["帐篷"].source.startswith("记忆:abcd1234")
