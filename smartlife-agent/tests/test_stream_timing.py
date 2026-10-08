"""流式关键路径耗时指标回归测试。"""

import asyncio

from app.agents.orchestrator_v2 import OrchestratorV2
from app.classifier import ClassificationResult
from app.streaming import EventQueue


class _Memory:
    def __init__(self):
        self.messages = []

    def add_message(self, thread_id, role, content):
        self.messages.append((thread_id, role, content))

    def get_history(self, thread_id, last_n=None):
        return self.messages


def _events(queue):
    async def run():
        await queue.finish()
        return [event async for event in queue]

    return asyncio.run(run())


def test_done_reports_ttft_response_ready_and_pipeline_timing():
    orchestrator = OrchestratorV2.__new__(OrchestratorV2)
    orchestrator._short_term_memory = _Memory()
    queue = EventQueue(trace_id="trace-timing")

    async def run():
        await queue.emit_token("你", step="generate")
        await queue.emit_token("好", step="generate")
        await orchestrator.finish_turn(
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
            queue,
        )

    asyncio.run(run())
    events = _events(queue)
    done = next(event for event in events if event.event == "done")
    timing = done.data["timing_ms"]

    assert timing["time_to_first_token_ms"] >= 0
    assert timing["response_ready_ms"] >= timing["time_to_first_token_ms"]
    assert timing["pipeline_ms"] >= timing["response_ready_ms"]
    assert done.data["response"] == "你好"
