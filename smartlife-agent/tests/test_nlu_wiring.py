"""NLU 改造到下游链路的接线：旅行不再误追问、装备按 1 人兜底。"""

from app.services.task_models import extract_shared_constraints
from app.trip_gear.constraints import derive_constraints
from app.trip_gear.gear import derive_gear
from app.trip_gear.itinerary import fallback_itinerary
from app.trip_gear.models import TripFacts


def test_travel_requirements_no_longer_lose_outdoor_destinations():
    """以前只认 14 个城市白名单，"去莫干山玩三天"会被要求补充目的地。"""
    requirements = extract_shared_constraints("去莫干山玩三天")

    assert requirements.destination == "莫干山"
    assert requirements.days == 3


def test_travel_requirements_understand_number_words():
    requirements = extract_shared_constraints("两位去三亚度假四天")

    assert requirements.destination == "三亚"
    assert requirements.days == 4
    assert requirements.people == 2


def test_unspecified_people_is_marked_not_guessed():
    facts, constraints = derive_constraints("去莫干山玩三天")

    assert facts.people is None
    assert facts.effective_people == 1
    texts = [item.text for item in constraints]
    assert "人数：未指定（按 1 人计）" in texts


def test_gear_quantities_use_one_person_when_people_is_unspecified():
    facts = TripFacts(destination="莫干山", days=2, people=None, lodging="camp")
    itinerary = fallback_itinerary(facts)

    gear = {item.name: item for item in derive_gear(facts, itinerary)}

    assert gear["睡袋"].qty == 1
    assert gear["帐篷"].qty == 1
