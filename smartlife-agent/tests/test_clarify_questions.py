"""追问卡片：问题、选项、以及把选项拼回一句可解析的话。"""

from app.services.clarify_questions import (
    build_questions,
    compose_answer,
    option_values,
    questions_has,
)
from app.trip_gear.constraints import derive_constraints


def test_missing_days_and_destination_each_get_a_question():
    from app.trip_gear.models import TripFacts

    questions = build_questions(["目的地", "天数"], TripFacts())

    assert [q["key"] for q in questions][:2] == ["destination", "days"]
    assert all(q["allow_custom"] for q in questions)
    # 目的地和天数至少各给三个可点选项
    assert len(questions[0]["options"]) >= 3
    assert len(questions[1]["options"]) >= 3


def test_province_question_offers_its_common_cities():
    facts, _ = derive_constraints("去广东玩一周")
    questions = build_questions(["具体城市"], facts)

    assert questions[0]["key"] == "cities"
    labels = [option["label"] for option in questions[0]["options"]]
    assert labels == ["广州", "深圳", "珠海"]


def test_questions_are_added_once_each():
    from app.trip_gear.models import TripFacts

    questions = build_questions(["目的地", "天数"], TripFacts())
    keys = [q["key"] for q in questions]

    assert len(keys) == len(set(keys))
    assert questions_has(questions, "people") and questions_has(questions, "budget")


def test_compose_answer_round_trips_through_slots():
    text = compose_answer([
        {"prefix": "目的地", "value": "杭州"},
        {"prefix": "天数", "value": "3 天"},
        {"prefix": "人数", "value": "2 人"},
        {"prefix": "预算", "value": "5000 元"},
    ])

    from app.nlu.slots import extract_slots

    slots = extract_slots(text, today="2026-11-09")
    assert text == "目的地：杭州；天数：3 天；人数：2 人；预算：5000 元"
    assert (slots.destination, slots.days, slots.people, slots.budget) == ("杭州", 3, 2, 5000)


def test_blank_custom_answers_are_skipped():
    text = compose_answer([
        {"prefix": "目的地", "value": "杭州"},
        {"prefix": "天数", "value": ""},
    ])

    assert text == "目的地：杭州"


def test_only_the_city_question_allows_multiple_choices():
    """多选只给"答案是集合"的题：天数/人数/预算/日期多选就是自相矛盾。"""
    facts, _ = derive_constraints("去广东玩一周")
    questions = build_questions(["具体城市", "天数"], facts)
    by_key = {question["key"]: question for question in questions}

    assert by_key["cities"]["multi"] is True
    assert by_key["cities"]["hint"]
    for key in ("days", "people", "budget", "start_date"):
        assert key in by_key
        assert by_key[key]["multi"] is False


def test_multi_city_answer_round_trips_into_a_city_list():
    from app.nlu.slots import extract_slots

    text = compose_answer([
        {"prefix": "目的地", "value": ["广州", "深圳", "珠海"]},
        {"prefix": "天数", "value": "3 天"},
    ])

    assert text == "目的地：广州、深圳、珠海；天数：3 天"
    slots = extract_slots(text, today="2026-11-09")
    assert slots.cities == ["广州", "深圳", "珠海"]
    assert slots.days == 3


def test_single_choice_answers_keep_working_after_multi_support():
    text = compose_answer([
        {"prefix": "目的地", "value": "杭州"},
        {"prefix": "人数", "value": ["2 人"]},
    ])

    assert text == "目的地：杭州；人数：2 人"


def test_origin_transport_and_lodging_get_their_own_questions():
    """出发地、交通、住宿都是必问项：没有默认值可退，只能问。"""
    from app.trip_gear.models import TripFacts

    questions = build_questions(["出发地", "交通", "住宿"], TripFacts())
    by_key = {question["key"]: question for question in questions}

    assert by_key["origin"]["answer_prefix"] == "出发地"
    assert len(by_key["origin"]["options"]) >= 3
    assert by_key["transport"]["answer_prefix"] == "交通"
    # 住宿只问每晚预算（默认酒店）：选项值仍是能直接解析的一句话
    lodging_values = [option["value"] for option in by_key["lodging"]["options"]]
    assert "酒店，每晚300元" in lodging_values
    assert not any("露营" in value for value in lodging_values)
    for question in (by_key["origin"], by_key["transport"], by_key["lodging"]):
        assert question["multi"] is False


def test_origin_transport_and_lodging_answers_are_parsed():
    """点完选项拼出来的那句话，要能还原成出发地/交通/住宿/每晚预算。"""
    from app.nlu.slots import extract_slots

    text = compose_answer([
        {"prefix": "出发地", "value": "上海"},
        {"prefix": "交通", "value": "坐高铁"},
        {"prefix": "住宿", "value": "酒店，每晚400元"},
    ])
    slots = extract_slots(text, today="2026-11-09")

    assert text == "出发地：上海；交通：坐高铁；住宿：酒店，每晚400元"
    assert slots.origin == "上海"
    assert slots.transport == "train"
    assert slots.lodging == "hotel"
    assert slots.lodging_budget == 400
    # "出发地：上海"里的上海不能被地名兜底扫成目的地
    assert slots.destination == ""


def test_every_option_value_fills_the_gap_it_belongs_to():
    """点选项 → 拼一句 → 重新派生，这一项必须真的被填上。

    回归背景：界面把**标签**当值拼回去（"住宿：经济型 ¥300/晚"），槽位解析不出来，
    于是同一张卡片一轮一轮问同一件事。选项的 `value` 必须是能被解析的写法，
    标签只给人看。
    """
    from app.trip_gear.constraints import derive_constraints
    from app.trip_gear.models import TripFacts

    from_question = {
        # key → 解析成功后 facts 上应当出现的字段
        "origin": lambda facts: facts.origin,
        "destination": lambda facts: facts.destination,
        "cities": lambda facts: facts.destination,
        "days": lambda facts: facts.days,
        "people": lambda facts: facts.people,
        "start_date": lambda facts: facts.start_date,
        "transport": lambda facts: facts.transport,
        "lodging": lambda facts: facts.lodging and facts.lodging_budget,
    }

    questions = build_questions(
        ["出发地", "目的地", "具体城市", "天数", "人数", "出发日期", "交通", "住宿"],
        TripFacts(),
    )
    assert {question["key"] for question in questions} >= set(from_question)

    for question in questions:
        check = from_question.get(question["key"])
        if check is None:
            continue
        assert question.get("options"), f"{question['key']} 应当有可点选项"
        mapping = option_values(question)
        for option in question["options"]:
            # 走"界面点标签 → 翻译成值"这条路，而不是直接用 value：
            # 这里就是要钉住"标签翻译成值"这一步不能省。
            text = compose_answer([{
                "prefix": question["answer_prefix"],
                "value": mapping[option["label"]],
            }])
            facts, _ = derive_constraints(text)
            assert check(facts), (
                f"{question['key']} 的选项「{option['label']}」拼成 {text!r} 后没被解析出来"
            )
