"""槽位补全兜底：开关、防幻觉、只填空缺。"""

import asyncio

import pytest

from app.services import slot_assist
from app.trip_gear.constraints import apply_assisted, derive_constraints
from app.trip_gear.models import Constraint, TripFacts


def test_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv(slot_assist.SWITCH_ENV, raising=False)
    assert slot_assist.is_enabled() is False


def test_switch_can_be_turned_on(monkeypatch):
    monkeypatch.setenv(slot_assist.SWITCH_ENV, "on")
    assert slot_assist.is_enabled() is True


def test_sanitize_rejects_hallucinated_destination():
    cleaned = slot_assist.sanitize(
        {"destination": "北海道"},
        message="去那个有竹子的地方待几天",
        missing=["destination", "days"],
    )
    assert "destination" not in cleaned


def test_sanitize_keeps_known_place_and_valid_numbers():
    cleaned = slot_assist.sanitize(
        {"destination": "莫干山", "days": 3, "people": 2, "budget": 1500},
        message="莫干山那边待几天",
        missing=["destination", "days", "people", "budget"],
    )
    assert cleaned == {"destination": "莫干山", "days": 3, "people": 2, "budget": 1500}


@pytest.mark.parametrize("value", [0, -1, 400, 61, None])
def test_sanitize_rejects_impossible_days(value):
    cleaned = slot_assist.sanitize({"days": value}, "随便玩几天", ["days"])
    assert "days" not in cleaned


def test_sanitize_drops_past_dates():
    cleaned = slot_assist.sanitize(
        {"start_date": "2020-01-01"}, "随便", ["start_date"], today="2026-11-06"
    )
    assert "start_date" not in cleaned


def test_assist_is_skipped_when_disabled(monkeypatch):
    monkeypatch.delenv(slot_assist.SWITCH_ENV, raising=False)

    async def boom(*_args, **_kwargs):
        raise AssertionError("开关关闭时不应该调用模型")

    monkeypatch.setattr(slot_assist, "ainvoke_json", boom)
    assert asyncio.run(slot_assist.assist_slots("去那边", ["destination"])) == {}


def test_assist_returns_cleaned_fields(monkeypatch):
    monkeypatch.setenv(slot_assist.SWITCH_ENV, "1")

    async def fake(*_args, **_kwargs):
        return slot_assist._AssistedSlots(destination="莫干山", days=3)

    monkeypatch.setattr(slot_assist, "ainvoke_json", fake)
    result = asyncio.run(
        slot_assist.assist_slots("去莫干山那边待几天", ["destination", "days"], llm=object())
    )

    assert result == {"destination": "莫干山", "days": 3}


def test_apply_assisted_only_fills_empty_slots():
    facts = TripFacts(destination="杭州", days=None)
    constraints = [Constraint(text="目的地：杭州", source="用户原话", key="destination")]

    merged, updated = apply_assisted(
        facts, constraints, {"destination": "莫干山", "days": 2}
    )

    assert merged.destination == "杭州"  # 不覆盖用户说的
    assert merged.days == 2
    assert any(item.source == "模型补全" and item.key == "days" for item in updated)


def test_apply_assisted_replaces_unspecified_people_placeholder():
    facts, constraints = derive_constraints("去莫干山玩三天")
    assert facts.people is None

    merged, updated = apply_assisted(facts, constraints, {"people": 2})

    assert merged.people == 2
    texts = [item.text for item in updated]
    assert "人数：未指定（按 1 人计）" not in texts
    assert "人数：2 人" in texts


def test_trip_gear_graph_uses_assist_before_asking():
    """规则抽不到目的地时，兜底能填上就直接继续，不再打断用户。"""
    from app.graphs.context import TurnContext
    from app.graphs.subgraphs.trip_gear import build_trip_gear_graph

    class Services:
        def get_chat_history(self, thread_id):
            return []

    async def planner(facts, constraints):
        from app.trip_gear.itinerary import fallback_itinerary

        return fallback_itinerary(facts)

    async def slot_assist(message, missing):
        return {"days": 2}

    ctx = TurnContext(services=Services(), trace_id="assist").with_extra(
        planner=planner,
        product_search=lambda query="", limit=5: [],
        slot_assist=slot_assist,
    )
    result = asyncio.run(build_trip_gear_graph().ainvoke(
        {
            "user_message": (
                "我一个人去石塘待几天，2026-11-10 出发，帮我看看带什么，"
                "从上海出发，坐高铁，住酒店每晚300元"
            ),
            "user_id": "u1",
            "thread_id": "t1",
        },
        context=ctx,
    ))

    assert result["status"] == "ok"
    assert result["facts"]["destination"] == "石塘"
    assert result["facts"]["days"] == 2
    assert any(item["source"] == "模型补全" for item in result["constraints"])
