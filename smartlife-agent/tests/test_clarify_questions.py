"""追问卡片：问题、选项、以及把选项拼回一句可解析的话。"""

from app.services.clarify_questions import build_questions, compose_answer, questions_has
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
