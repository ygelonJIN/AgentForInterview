"""travel（with_gear=False）没有 gear 节点，读装备时必须容错。

回归背景：verify / repair / verify_clarify / finalize 原来直接 ``state["gear"]``，
travel 链路里这个键根本不存在，于是走一遍规划就 ``KeyError: 'gear'``。
"""

import asyncio

from app.graphs.context import TurnContext
from app.graphs.subgraphs.travel import build_travel_graph
from app.graphs.subgraphs.trip_gear import _gear_items


class _Services:
    def get_chat_history(self, thread_id):
        return []


def test_gear_items_tolerates_missing_key():
    assert _gear_items({}) == []


def _run_travel(message: str):
    events = []
    graph = build_travel_graph()
    ctx = TurnContext(services=_Services(), trace_id="travel-no-gear", bound_writer=events.append)
    state = asyncio.run(graph.ainvoke(
        {
            "user_message": message,
            "thread_id": "travel-no-gear",
            "user_id": "u",
            "classification": {"intent": "travel", "route": "plan_and_execute", "sub_intent": "plan_trip"},
        },
        context=ctx,
    ))
    return state, events


def test_travel_plan_runs_without_a_gear_key():
    state, events = _run_travel("去杭州玩两天，一个人，明天出发")

    assert state["status"] == "ok"
    plan = [event for event in events if event.get("event") == "plan_result"][-1]["data"]
    assert plan["with_gear"] is False
    assert plan["gear"] == []


def test_travel_progress_reports_four_steps_and_no_gear_copy():
    _state, events = _run_travel("去杭州玩两天，一个人，明天出发")

    steps = [
        (event["data"]["step"], event["data"]["total"], event["data"]["description"])
        for event in events
        if event.get("event") == "step"
    ]
    assert [(step, total) for step, total, _ in steps] == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert not any("装备建议" in text for _, _, text in steps)


def test_clarify_timeout_leaves_room_for_external_facts():
    """追问补答会重跑 _load_facts，单条事实最坏吃到 FACT_TIMEOUT_SECONDS。

    回归背景：clarify 原本 timeout=5，而事实单条上限 8s，provider 一慢
    补答那步就 NodeTimeoutError，整轮失败。
    """
    from app.graphs.subgraphs.trip_gear import FACT_TIMEOUT_SECONDS, NODE_TIMEOUTS

    assert NODE_TIMEOUTS["clarify"] > FACT_TIMEOUT_SECONDS
    assert NODE_TIMEOUTS["derive"] > FACT_TIMEOUT_SECONDS
