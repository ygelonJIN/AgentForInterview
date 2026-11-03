"""旅行子图的步骤广播与修订正文替换测试。

契约：STEP 2 只发一次、STEP 3 只发一次；修订前先发 ``response_reset``，
保证 UI 不会把旧版正文和新版正文拼在一起。
"""
import asyncio

from app.graphs.context import TurnContext
from app.graphs.subgraphs.travel import build_travel_plan_graph
from app.streaming import EventType


class _Planner:
    def __init__(self):
        self.calls = 0

    async def __call__(self, request, context, previous_plan=None, feedback=None, on_token=None):
        self.calls += 1
        text = "修订版：一、二、三" if previous_plan else "初版：一、二、三"
        if on_token:
            await on_token(text, "revise" if previous_plan else "plan")
        return text


class _Reflector:
    def __init__(self):
        self.calls = 0

    async def __call__(self, request, plan):
        self.calls += 1
        return {"is_satisfactory": False, "suggestions": ["继续修订"]} if self.calls == 1 \
            else {"is_satisfactory": True, "suggestions": []}


class _TravelService:
    def _build_plan_context(self, user_input, user_id, chat_history=None, constraints=""):
        return "攻略与偏好"

    @staticmethod
    def _remove_revision_notes(plan):
        return plan


class _Services:
    travel_service = _TravelService()
    llm = None

    def get_chat_history(self, thread_id):
        return []


def _run():
    events = []
    ctx = TurnContext(services=_Services(), trace_id="travel:test").with_extra(
        planner=_Planner(),
        reflector=_Reflector(),
        executor=lambda step, state: {"status": "completed", "result": "ok"},
        max_revisions=3,
    ).bind_writer(events.append)
    state = asyncio.run(build_travel_plan_graph().ainvoke(
        {"user_message": "我要去上海旅游，3000块预算玩5天", "user_id": "user-1", "thread_id": "t"},
        context=ctx,
    ))
    return state, events


def test_travel_plan_emits_steps_two_and_three_and_resets_before_revision():
    state, events = _run()

    assert state["response"].startswith("修订版")
    steps = [event["data"]["step"] for event in events if event["event"] == EventType.STEP]
    assert steps == [2, 3], "STEP 2/3 各只广播一次"

    types = [event["event"] for event in events]
    reset_index = types.index(EventType.RESPONSE_RESET)
    revised_index = next(
        index for index, event in enumerate(events)
        if event["event"] == EventType.TOKEN and event["data"]["token"].startswith("修订版")
    )
    assert reset_index < revised_index


def test_travel_plan_tokens_carry_stage_as_step():
    _, events = _run()
    token_steps = {
        event["step"] for event in events if event["event"] == EventType.TOKEN
    }
    assert {"plan", "revise"} <= token_steps
