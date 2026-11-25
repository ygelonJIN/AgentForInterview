"""预算内缩减：系统只提议"减内容"，不提议"放宽预算"。"""

from app.graphs.subgraphs.trip_gear import _apply_budget_cut
from app.trip_gear.models import DayPlan, Itinerary, ItineraryItem, TripFacts


def _state(costs=(800.0, 600.0)):
    itinerary = Itinerary(
        destination="杭州",
        days=[
            DayPlan(day=index + 1, lodging="hotel", items=[
                ItineraryItem(time="10:00", activity=f"活动{index + 1}", duration_min=120, cost=cost)
            ], lodging_cost=400)
            for index, cost in enumerate(costs)
        ],
    )
    return {"itinerary": itinerary.model_dump()}


def test_少玩一天真的删掉一天():
    facts = TripFacts(destination="杭州", days=2, budget=1500)
    result = _apply_budget_cut("少玩一天", _state(), facts)
    assert result is not None
    assert result["cut"] == "day"
    assert len(result["itinerary"]["days"]) == 1
    assert facts.days == 1


def test_砍掉最贵的一项():
    facts = TripFacts(destination="杭州", days=2, budget=1500)
    result = _apply_budget_cut("砍掉最贵的一项", _state(), facts)
    assert result is not None
    assert result["cut"] == "item"
    remaining = [item["cost"] for day in result["itinerary"]["days"] for item in day["items"]]
    assert 800.0 not in remaining
    assert 600.0 in remaining


def test_只剩一天时不能再减():
    facts = TripFacts(destination="杭州", days=1, budget=1500)
    assert _apply_budget_cut("少玩一天", _state(costs=(800.0,)), facts) is None


def test_没说要减就不动():
    facts = TripFacts(destination="杭州", days=2, budget=1500)
    assert _apply_budget_cut("预算 2000 元", _state(), facts) is None
