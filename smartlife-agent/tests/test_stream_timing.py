"""流式关键路径耗时指标回归测试（事件出口已改为 LangGraph stream writer）。"""

import asyncio

from app.classifier import ClassificationResult
from app.graphs.events import EventSink, TurnTiming
from app.services.turn_services import TurnServices


class _Memory:
    def __init__(self):
        self.messages = []

    def add_message(self, thread_id, role, content):
        self.messages.append((thread_id, role, content))

    def get_history(self, thread_id, last_n=None):
        return self.messages


def test_done_reports_ttft_response_ready_and_pipeline_timing():
    services = TurnServices.__new__(TurnServices)
    services._short_term_memory = _Memory()
    collected = []
    sink = EventSink(collected.append, TurnTiming(), trace_id="trace-timing")

    async def run():
        await sink.emit_token("你", step="generate")
        await sink.emit_token("好", step="generate")
        await services.finish_turn(
            "thread-1",
            "你好",
            ClassificationResult(
                route="react",
                intent="general",
                intents=["general"],
                sub_intent="chat",
                retrieval="none",
                confidence=1.0,
                reason="test",
            ),
            sink,
        )

    asyncio.run(run())
    done = next(event for event in collected if event["event"] == "done")
    timing = done["data"]["timing_ms"]

    assert timing["time_to_first_token_ms"] >= 0
    assert timing["response_ready_ms"] >= timing["time_to_first_token_ms"]
    assert timing["pipeline_ms"] >= timing["response_ready_ms"]
    assert done["data"]["response"] == "你好"


def test_event_sink_writes_custom_chunk_shape_expected_by_ui():
    collected = []
    sink = EventSink(collected.append, TurnTiming(), trace_id="trace-shape")

    asyncio.run(sink.emit_token("你", step="generate", section="plan"))

    payload = collected[0]
    assert set(payload) == {"event", "data", "step", "timestamp", "trace_id"}
    assert payload["event"] == "token"
    assert payload["data"] == {"token": "你", "section": "plan"}
    assert payload["step"] == "generate"
    assert payload["trace_id"] == "trace-shape"
