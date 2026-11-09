"""用户指定活动（FixedEvent）的抽取、约束派生与钉入行程。

对应需求："我想参与一点活动 / 我已经买好票了，11 号晚上去上海音乐厅"。
核心是两件事：不能把"想参加一点活动"编成一条日程；已订的活动不能被行程挤掉。
"""

from app.nlu.slots import extract_slots
from app.trip_gear.constraints import derive_constraints
from app.trip_gear.itinerary import (
    DAY_WINDOW_MIN,
    apply_fixed_events,
    fallback_itinerary,
    validate,
)
from app.trip_gear.models import DayPlan, FixedEvent, Itinerary, ItineraryItem, TripFacts


def test_wants_activity_without_naming_anything_creates_no_schedule():
    slots = extract_slots("我要去上海玩两天，3000预算，我住在家，想参与一点活动")

    assert slots.wants_activity is True
    assert slots.fixed_events == []


def test_booked_event_is_extracted_with_day_time_and_price():
    slots = extract_slots("上海2天，已订好第2天下午的陶艺工作坊，200元")

    assert len(slots.fixed_events) == 1
    event = slots.fixed_events[0]
    assert event["title"] == "陶艺工作坊"
    assert event["day"] == 2
    assert event["cost"] == 200.0
    assert event["status"] == "booked"


def test_venue_is_split_from_title():
    slots = extract_slots("上海2天，已经买票了，在上海音乐厅看音乐会")

    event = slots.fixed_events[0]
    assert event["title"] == "音乐会"
    assert event["venue"] == "上海音乐厅"


def test_unbooked_wish_is_not_a_fixed_event():
    """"想去上海玩两天" 里的"想去"是目的地，不是活动。"""
    slots = extract_slots("我想去上海玩两天")

    assert slots.fixed_events == []


def test_constraints_mark_booked_event_hard_and_wish_soft():
    facts, constraints = derive_constraints(
        "上海2天3000元，已订好第2天19:30的陶艺工作坊，200元，想参加活动"
    )

    assert facts.wants_activity is True
    assert [event.title for event in facts.fixed_events] == ["陶艺工作坊"]

    by_key = {}
    for constraint in constraints:
        by_key.setdefault(constraint.key, []).append(constraint)
    assert by_key["wants_activity"][0].hard is False
    assert by_key["fixed_event"][0].hard is True
    assert "陶艺工作坊" in by_key["fixed_event"][0].text


def test_fixed_event_is_pinned_and_return_leg_is_dropped():
    facts = TripFacts(
        destination="上海",
        days=2,
        fixed_events=[FixedEvent(title="陶艺工作坊", day=2, start="19:30", cost=200, status="booked")],
    )
    itinerary = fallback_itinerary(facts)
    second_day = itinerary.days[1]

    pinned = [item for item in second_day.items if item.activity.startswith("陶艺工作坊")]
    assert len(pinned) == 1
    assert pinned[0].time == "19:30"
    assert pinned[0].cost == 200.0
    # 那天晚上还有活动，就不该排"返程"
    assert not any("返程" in item.activity for item in second_day.items)


def test_event_without_day_is_not_pinned():
    """没说是第几天，就别猜——猜等于替用户决定哪天去。"""
    facts = TripFacts(
        destination="上海",
        days=2,
        fixed_events=[FixedEvent(title="陶艺工作坊", day=0, status="booked")],
    )
    itinerary = fallback_itinerary(facts)

    assert not any(
        "陶艺工作坊" in item.activity
        for day in itinerary.days
        for item in day.items
    )


def test_pinned_event_survives_truncation():
    """当天排满时，被删的是可调整条目，不是已订的活动。"""
    facts = TripFacts(
        destination="上海",
        days=1,
        fixed_events=[FixedEvent(title="音乐会", day=1, start="19:30", duration_min=120, status="booked")],
    )
    packed = Itinerary(
        destination="上海",
        days=[DayPlan(day=1, items=[
            ItineraryItem(time="08:00", activity="上午安排", duration_min=400, cost=0),
            ItineraryItem(time="15:00", activity="下午安排", duration_min=400, cost=0),
        ])],
    )
    result = apply_fixed_events(packed, facts)
    items = result.days[0].items

    assert any("音乐会" in item.activity for item in items)
    assert sum(item.duration_min for item in items) <= DAY_WINDOW_MIN


def test_planned_event_is_not_pinned():
    """只是"想去"的活动排不下就算了，不能挤掉别人的安排。"""
    facts = TripFacts(
        destination="上海",
        days=2,
        fixed_events=[FixedEvent(title="音乐会", day=1, status="planned")],
    )
    itinerary = fallback_itinerary(facts)

    assert not any(
        "音乐会" in item.activity for day in itinerary.days for item in day.items
    )


def test_appointment_without_day_takes_the_middle_day():
    """约朋友没说哪天：中间那天——第一天赶路、最后一天返程，都不合适。"""
    facts = TripFacts(
        destination="上海",
        days=3,
        fixed_events=[FixedEvent(title="找朋友", venue="浦东", status="appointment", duration_min=180)],
    )
    itinerary = fallback_itinerary(facts)

    day2 = itinerary.days[1]
    assert any("找朋友" in item.activity and "浦东" in item.activity for item in day2.items)
    assert not any(
        "找朋友" in item.activity
        for day in (itinerary.days[0], itinerary.days[2])
        for item in day.items
    )
    # 半天占掉之后不能再和别的安排撞时段
    assert not validate(itinerary)


def test_appointment_day_is_respected_when_given():
    facts = TripFacts(
        destination="上海",
        days=3,
        fixed_events=[FixedEvent(title="见同学", venue="静安", day=3, status="appointment")],
    )
    itinerary = fallback_itinerary(facts)

    assert any("见同学" in item.activity for item in itinerary.days[2].items)


def test_origin_and_appointment_in_one_sentence():
    """"我在广东，我要去上海，有一天去找浦东的朋友"：起点、终点、那天都得对。"""
    facts, constraints = derive_constraints(
        "我在广东，我要去上海玩三天，有一天去找一个我在浦东的朋友"
    )

    assert (facts.origin, facts.destination) == ("广东", "上海")
    by_key = {item.key: item for item in constraints}
    assert by_key["origin"].text == "出发地：广东"
    assert by_key["destination"].text == "目的地：上海"
    # 约见只锁"哪天要有"，哪天排由排程决定，所以是软约束
    assert by_key["appointment"].hard is False
    assert "浦东" in by_key["appointment"].text
