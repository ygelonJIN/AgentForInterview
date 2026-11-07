"""修复确认：需要用户点头的改法（如露营改住民宿）必须先问、同意才执行。"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.graphs.context import TurnContext
from app.graphs.subgraphs.trip_gear import (
    TripGearState,
    _accepts_confirm,
    after_verify,
    repair_confirm,
)
from app.trip_gear.models import DayPlan, GearItem, Itinerary, ItineraryItem

CONFIRM_ACTION = {
    "kind": "change_lodging",
    "name": "露营改住民宿",
    "saving": 825.0,
    "reason": "不买帐篷省 1375 元，住宿多花 550 元，净省 825 元；但这改变了你原本的露营安排",
    "requires_confirm": True,
    "meta": {"from": "camp", "to": "hotel", "nights": 1},
}


class _Services:
    def get_chat_history(self, thread_id):
        return []


def _state(**overrides):
    state = {
        "facts": {"destination": "杭州", "days": 1, "people": 1, "lodging": "camp"},
        "gear": [
            GearItem(name="帐篷", qty=1, status="need_buy").model_dump(),
            GearItem(name="睡袋", qty=1, status="need_buy").model_dump(),
        ],
        "products": {
            "帐篷": {"name": "双人帐篷", "price": 599},
            "睡袋": {"name": "四季睡袋", "price": 299},
        },
        "itinerary": Itinerary(
            destination="杭州",
            days=[DayPlan(
                day=1,
                items=[ItineraryItem(time="10:00", activity="到达", duration_min=60, cost=0)],
                lodging="camp",
                lodging_cost=50,
            )],
        ).model_dump(),
        "confirm_options": [CONFIRM_ACTION],
        "confirm_rounds": 0,
        "revision": 0,
    }
    state.update(overrides)
    return state


def _run(state, **extra):
    graph = StateGraph(TripGearState, context_schema=TurnContext)
    graph.add_node("repair_confirm", repair_confirm)
    graph.add_edge(START, "repair_confirm")
    graph.add_edge("repair_confirm", END)
    compiled = graph.compile(checkpointer=InMemorySaver())
    ctx = TurnContext(services=_Services(), trace_id="confirm-test").with_extra(**extra)
    cfg = {"configurable": {"thread_id": "confirm-thread"}}
    first = asyncio.run(compiled.ainvoke(state, cfg, context=ctx))
    return compiled, cfg, ctx, first


def test_after_verify_asks_when_confirmation_is_required():
    state = _state(verdict={"state": "needs_input"}, confirm_options=[CONFIRM_ACTION])

    assert after_verify(state) == "repair_confirm"


def test_after_verify_asks_only_once():
    state = _state(
        verdict={"state": "needs_input"},
        confirm_options=[CONFIRM_ACTION],
        confirm_rounds=1,
    )

    assert after_verify(state) != "repair_confirm"


def test_auto_repair_still_wins_over_confirmation():
    state = _state(
        verdict={"state": "repairable"},
        repair_options=[{"kind": "skip", "name": "睡袋", "saving": 299}],
        confirm_options=[CONFIRM_ACTION],
    )

    assert after_verify(state) == "repair"


def test_confirm_question_stops_and_names_the_tradeoff():
    _compiled, _cfg, _ctx, first = _run(_state())

    interrupts = first.get("__interrupt__") or []
    assert interrupts, "需要确认的改法必须停下来问"
    payload = interrupts[0].value
    assert payload["type"] == "trip_gear_repair_confirm"
    assert "净省 825 元" in payload["question"]
    assert "调整" in payload["question"]


def test_accepting_applies_lodging_swap_and_drops_camping_gear():
    compiled, cfg, ctx, first = _run(_state())
    assert first.get("__interrupt__")

    state = asyncio.run(compiled.ainvoke(Command(resume="调整，改住民宿吧"), cfg, context=ctx))

    assert state["status"] == "confirmed"
    assert all(day["lodging"] == "hotel" for day in state["itinerary"]["days"])
    assert state["products"] == {}
    assert all(item["status"] == "skipped" for item in state["gear"])
    assert state["confirm_options"] == []
    assert state["confirm_rounds"] == 1
    assert state["revision"] == 1


def test_declining_keeps_original_plan():
    compiled, cfg, ctx, first = _run(_state())
    assert first.get("__interrupt__")

    state = asyncio.run(compiled.ainvoke(Command(resume="不用，保持原样"), cfg, context=ctx))

    assert state["status"] == "confirm_declined"
    assert state["itinerary"]["days"][0]["lodging"] == "camp"
    assert state["products"]
    assert state["revision"] == 0


def test_confirm_reply_parsing_prefers_negative_words():
    assert _accepts_confirm("调整")
    assert _accepts_confirm("可以，改住民宿")
    assert not _accepts_confirm("不用")
    assert not _accepts_confirm("不要改")
    assert not _accepts_confirm("保持原样")
    assert not _accepts_confirm("")


def test_interrupt_payload_keeps_confirm_type_for_ui_routing():
    """UI 靠中断载荷里的 type 区分"补槽位"和"修复确认"，不能被压成字符串。"""
    from types import SimpleNamespace

    from app.graphs.orchestrator import _interrupt_payload

    payload = _interrupt_payload({
        "__interrupt__": [SimpleNamespace(value={
            "type": "trip_gear_repair_confirm",
            "question": "要不要改住民宿？",
            "action": CONFIRM_ACTION,
        })]
    })

    assert payload["type"] == "trip_gear_repair_confirm"
    assert payload["question"] == "要不要改住民宿？"
