"""旅游流式步骤和修订正文替换测试。"""

import asyncio

from app.agents.travel_agent import TravelAgent
from app.streaming import EventType


class _Queue:
    def __init__(self):
        self.events = []

    async def emit(self, event_type, data, step=""):
        self.events.append({
            "event": getattr(event_type, "value", event_type),
            "data": data,
            "step": step,
        })

    async def emit_thinking(self, message, step="", model=""):
        await self.emit(EventType.THINKING, {"message": message}, step)

    async def emit_token(self, token, step=""):
        await self.emit(EventType.TOKEN, {"token": token}, step)

    async def emit_tool_call(self, tool_name, tool_input=None, step=""):
        await self.emit(EventType.TOOL_CALL, {"tool": tool_name, "input": tool_input}, step)

    async def emit_tool_result(self, tool_name, output, step=""):
        await self.emit(EventType.TOOL_RESULT, {"tool": tool_name, "output": output}, step)

    async def emit_response_reset(self, step=""):
        await self.emit(EventType.RESPONSE_RESET, {}, step)


def test_travel_plan_emits_steps_two_and_three_and_resets_before_revision(monkeypatch):
    class _FakeGraph:
        def __init__(self, **kwargs):
            self.on_event = kwargs["on_event"]
            self.on_token = kwargs["on_token"]

        async def run_streaming(self, *args, **kwargs):
            await self.on_event("plan_started", {"revision": 0})
            await self.on_token("初版：一、二、三、四、五、六", "plan")
            await self.on_event("reflection", {"is_satisfactory": False})
            await self.on_event("revision", {"revision_count": 1})
            await self.on_token("修订版：一、二、三、四、五、六", "revise")
            return {"final_plan": "修订版：一、二、三、四、五、六"}

    monkeypatch.setattr(
        "app.agents.travel_agent.TravelPlanGraph",
        _FakeGraph,
    )
    agent = TravelAgent.__new__(TravelAgent)
    agent._build_plan_context = lambda *_args, **_kwargs: "攻略与偏好"
    agent.travel_tool_executor = type("FakeExecutor", (), {
        "get_weather_evidence": staticmethod(
            lambda *_args, **_kwargs: __import__("asyncio").sleep(
                0, {"accepted": False, "simulated": False, "error": "unavailable"}
            )
        )
    })()
    queue = _Queue()

    result = asyncio.run(agent.plan_trip_streaming(
        "我要去上海旅游，3000块预算玩5天",
        "user-1",
        queue=queue,
    ))

    assert result == "修订版：一、二、三、四、五、六"
    step_numbers = [
        event["data"]["step"]
        for event in queue.events
        if event["event"] == EventType.STEP
    ]
    assert step_numbers == [2, 3]

    event_types = [event["event"] for event in queue.events]
    reset_index = event_types.index(EventType.RESPONSE_RESET)
    revised_token_index = next(
        index
        for index, event in enumerate(queue.events)
        if event["event"] == EventType.TOKEN
        and event["data"]["token"].startswith("修订版")
    )
    assert reset_index < revised_token_index
