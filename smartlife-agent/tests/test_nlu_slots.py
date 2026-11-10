"""槽位抽取：预算、人数、天数、目的地、日期，以及两套入口的一致性。"""

import pytest

from app.nlu.slots import (
    extract_slots,
    parse_budget_detail,
    parse_days,
    parse_destination,
    parse_origin,
    parse_people,
    parse_start_date,
)
from app.services.task_models import extract_shared_constraints
from app.trip_gear.constraints import derive_constraints


@pytest.mark.parametrize(
    "text,expected",
    [
        ("预算3000", 3000),
        ("预算一万块", 10000),
        ("预算 1.5万", 15000),
        ("1万5", 15000),
        ("3千", 3000),
        ("预算三千", 3000),
        ("三千预算", 3000),
        ("预算五千", 5000),
        ("2k", 2000),
        ("1.5w", 15000),
        ("5000以内", 5000),
        ("1500左右", 1500),
        ("¥1,000", 1000),
        ("不超过8000元", 8000),
        ("预算10000", 10000),
        # 中文数字 + 万/亿：漏了这两个字符，"预算一万"会被读成 1 元，
        # "一万预算"直接抽不到。
        ("预算一万", 10000),
        ("一万预算", 10000),
        ("预算一万五", 15000),
        ("预算两万", 20000),
        ("预算两万三千", 23000),
        ("周末杭州两天，预算一万", 10000),
    ],
)
def test_budget_formats(text, expected):
    assert parse_budget_detail(text)[0] == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # 商品单价不能顶掉行程预算
        ("预算3000，手机5000元", 3000),
        ("手机5000，预算3000", 3000),
        # 装备预算单列，不能被当成总预算
        ("装备预算1000，总预算3000", 3000),
        ("推荐800元以内的跑鞋，并规划杭州两天行程，预算3000元，2人出行", 3000),
    ],
)
def test_budget_is_not_hijacked_by_item_or_gear_price(text, expected):
    assert parse_budget_detail(text)[0] == expected


def test_gear_budget_is_kept_separate():
    slots = extract_slots("去临安露营两天，装备预算500")
    assert slots.gear_budget == 500
    assert slots.budget is None
    assert slots.budget_scope == "trip_only"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("两位", 2),
        ("两个人", 2),
        ("3人", 3),
        ("四个人", 4),
        ("一家三口", 3),
        ("我们一家三口", 3),
        ("2大1小", 3),
        ("我和老婆", 2),
        ("情侣", 2),
        ("一个人", 1),
        ("下周三去上海出差两天", None),
        ("我们", None),
    ],
)
def test_people(text, expected):
    assert parse_people(text) == expected


def test_people_unspecified_is_none_not_one():
    assert parse_people("去杭州玩三天") is None
    constraints = extract_shared_constraints("去杭州玩三天")
    assert constraints.people is None
    assert constraints.effective_people == 1


@pytest.mark.parametrize(
    "text,expected",
    [
        ("7天", 7),
        ("七天", 7),
        ("一周", 7),
        ("两周", 14),
        ("半个月", 15),
        ("周末", 2),
        ("3晚", 4),
        ("三天两晚", 3),
        ("四日游", 4),
        ("两个月", 60),
        ("8月1日", None),  # 日期不是天数
    ],
)
def test_days(text, expected):
    assert parse_days(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("去莫干山露营", "莫干山"),
        ("去杭州玩三天", "杭州"),
        ("去日本玩五天", "日本"),
        ("在杭州工作", "杭州"),
        ("去上海出差两天", "上海"),
        ("杭州两天行程", "杭州"),
        ("从北京到上海", "上海"),
        ("推荐800元以内的跑鞋，并规划杭州两天行程", "杭州"),
        # "我"后面的"在"是出发地，不是目的地：同一句里"去上海"必须赢
        ("我在广东，我要去上海玩三天", "上海"),
        ("我在广东，去上海玩三天", "上海"),
        ("我在杭州，周末想去莫干山露营", "莫干山"),
        ("从广州飞上海", "上海"),
        ("去露营", ""),  # 场景词不是地名
        ("我想买个帐篷", ""),
    ],
)
def test_destination(text, expected):
    assert parse_destination(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我在广东，我要去上海玩三天", "广东"),
        ("我在上海，想去迪士尼玩两天", "上海"),
        ("从广州出发去上海", "广州"),
        ("从广州飞上海", "广州"),
        ("上海出发去广东玩一周", "上海"),  # 口语里最常见的写法，没有"从/在"
        ("后天出发去杭州", ""),  # "后天"不是地名：裸"X出发"只认地名表里的词
        ("我在浦东的朋友那边住一晚", ""),  # "的"后面是半截句子，不是地名
        ("去杭州玩三天", ""),
        ("预算3000", ""),
    ],
)
def test_origin(text, expected):
    assert parse_origin(text) == expected


def test_visit_is_an_appointment_not_a_ticket():
    """"有一天去找朋友"是约见：地点和事由固定，哪天由排程决定。"""
    slots = extract_slots("我在广东，我要去上海玩三天，有一天去找一个我在浦东的朋友")

    assert slots.destination == "上海"
    assert slots.origin == "广东"
    assert len(slots.fixed_events) == 1
    event = slots.fixed_events[0]
    assert event["title"] == "找朋友"
    assert event["venue"] == "浦东"
    assert event["status"] == "appointment"
    assert event["day"] == 0


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2025-08-01 出发", "2025-08-01"),
        ("8月1日出发", "2027-08-01"),  # 今年的 8/1 已过，滚到明年
        ("明天出发", "2026-11-07"),
        ("后天出发", "2026-11-08"),
        ("下周三出发", "2026-11-11"),
        ("下周六出发", "2026-11-14"),
        ("本周六出发", "2026-11-07"),
        ("周末去", "2026-11-07"),
    ],
)
def test_start_date(text, expected):
    assert parse_start_date(text, today="2026-11-06") == expected


def test_shared_constraints_and_trip_facts_agree():
    """两条链路必须从同一句话里抽出同样的值——这是这次改动的核心。"""
    message = "两位去莫干山玩三天，预算3000"
    shared = extract_shared_constraints(message)
    facts, _ = derive_constraints(message)

    assert shared.destination == facts.destination == "莫干山"
    assert shared.days == facts.days == 3
    assert shared.people == facts.people == 2
    assert shared.budget == facts.budget == 3000


def test_shared_constraints_carry_extra_requirements():
    constraints = extract_shared_constraints("2大1小去三亚，住民宿")
    assert constraints.people == 3
    assert constraints.destination == "三亚"
    assert "同行有儿童" in constraints.requirements
    assert "住宿：酒店/民宿" in constraints.requirements


def test_budget_recognised_when_number_comes_first():
    """"3000预算" 是中文口语最常见写法，原先只认 "预算3000"。"""
    from app.nlu.slots import extract_slots

    for text in (
        "上海2天，预算3000",
        "上海2天，3000预算",
        "上海2天，3000块预算",
        "上海2天，一共3000",
        "上海2天，总共3000元",
    ):
        assert extract_slots(text).budget == 3000.0, text


def test_budget_still_excludes_gear_and_product_prices():
    from app.nlu.slots import extract_slots

    slots = extract_slots("装备预算1000，总预算3000")
    assert slots.budget == 3000.0
    assert slots.gear_budget == 1000.0

    # 手机单价不是出行预算
    assert extract_slots("上海2天，手机5000元").budget is None


def test_nights_as_hotel_rate_is_not_trip_length():
    """"酒店一晚不超400"里的"一晚"是房价单位，不能把一周行程算成两天。"""
    text = "我有两个人要在上海出发去广东，打算一周，去广东的很多城市，预算酒店一晚不超400"

    slots = extract_slots(text)

    assert slots.days == 7
    assert slots.lodging_budget == 400
    assert slots.destination == "广东"
    assert slots.origin == "上海"
    assert slots.people == 2


@pytest.mark.parametrize(
    "text,expected",
    [
        ("每晚不超过300", 300),
        ("住宿一晚500以内", 500),
        ("酒店预算5000", None),   # 整趟预算，不是每晚
        ("玩三晚", None),
    ],
)
def test_lodging_budget(text, expected):
    from app.nlu.slots import parse_lodging_budget

    assert parse_lodging_budget(text) == expected
