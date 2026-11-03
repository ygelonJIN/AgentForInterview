"""旅行子图（纯 LangGraph）的步骤、执行、反思和修订闭环测试。

依赖通过 ``TurnContext.extra`` 注入，节点签名统一 ``(state, runtime)``。
"""
import asyncio
import re

from app.graphs.context import TurnContext
from app.graphs.subgraphs.travel import build_travel_plan_graph
from app.observability import get_trace_recorder


class _Planner:
    def __init__(self):
        self.calls = []

    async def __call__(self, request, context, previous_plan=None, feedback=None, on_token=None):
        self.calls.append((request, previous_plan, feedback))
        suffix = "修订版" if previous_plan else "初版"
        day_match = re.search(r"(\d+)\s*天", request)
        days = int(day_match.group(1)) if day_match else 3
        lines = [f"第{index + 1}天：行程{index + 1}" for index in range(days)]
        plan = f"{suffix}：\n" + "\n".join(lines)
        if on_token:
            await on_token(plan, "plan" if previous_plan is None else "revise")
        return plan


class _Reflector:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    async def __call__(self, request, plan):
        return self.outcomes.pop(0)


class _Executor:
    def __init__(self, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    async def __call__(self, step, state):
        self.calls.append(step["id"])
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("step failed")
        return {"status": "completed", "result": f"done:{step['id']}", "source": "test"}


class _TravelService:
    def _build_plan_context(self, user_input, user_id, chat_history=None, constraints=""):
        return "上下文"

    @staticmethod
    def _remove_revision_notes(plan):
        return plan


class _Services:
    travel_service = _TravelService()
    llm = None

    def get_chat_history(self, thread_id):
        return []


def _run(request, *, planner, reflector, executor, max_revisions=3, chat_history=None):
    ctx = TurnContext(services=_Services(), trace_id="travel:test").with_extra(
        planner=planner,
        reflector=reflector,
        executor=executor,
        max_revisions=max_revisions,
        chat_history=chat_history,
    )
    return asyncio.run(build_travel_plan_graph().ainvoke(
        {"user_message": request, "user_id": "user-1", "thread_id": "travel:test"},
        context=ctx,
    ))


def test_travel_graph_executes_steps_and_revises_until_approved():
    planner = _Planner()
    executor = _Executor()
    state = _run(
        "杭州3天预算1000元",
        planner=planner,
        reflector=_Reflector([
            {"is_satisfactory": False, "suggestions": ["增加休息时间"]},
            {"is_satisfactory": True, "suggestions": []},
        ]),
        executor=executor,
        max_revisions=3,
        chat_history=[{"role": "user", "content": "从上海出发"}],
    )

    assert state["requirements"]["destination"] == "杭州"
    assert state["requirements"]["days"] == 3
    assert state["chat_history"] == [{"role": "user", "content": "从上海出发"}]
    assert state["revision_count"] == 1
    assert len(state["plan_versions"]) == 2
    assert len(state["evidence_steps"]) == 2
    assert [step["status"] for step in state["evidence_results"]] == ["completed"] * 2
    assert state["status"] == "approved"
    assert len(planner.calls) == 2
    assert len(executor.calls) == 2


def test_travel_graph_stops_at_max_revisions_with_best_effort_status():
    state = _run(
        "北京2天预算800元",
        planner=_Planner(),
        reflector=_Reflector([
            {"is_satisfactory": False, "suggestions": ["仍需修改"]},
            {"is_satisfactory": False, "suggestions": ["仍需修改"]},
        ]),
        executor=_Executor(),
        max_revisions=1,
    )

    assert state["revision_count"] == 1
    assert len(state["plan_versions"]) == 2
    assert state["status"] == "max_revisions_reached"


def test_travel_graph_requires_clarification_for_missing_core_fields():
    state = _run(
        "帮我规划一次旅行",
        planner=_Planner(),
        reflector=_Reflector([{"is_satisfactory": True}]),
        executor=_Executor(),
    )

    assert state["status"] == "needs_clarification"
    assert "目的地" in state["final_plan"]
    assert "天数" in state["final_plan"]
    assert not state.get("plan_versions")


def test_travel_graph_records_executor_failure_without_crashing():
    state = _run(
        "上海2天预算500元",
        planner=_Planner(),
        reflector=_Reflector([
            {"is_satisfactory": False, "suggestions": ["修复执行问题"]},
            {"is_satisfactory": True, "suggestions": []},
        ]),
        executor=_Executor(fail_first=True),
        max_revisions=3,
    )

    failed = [step for step in state["evidence_results"] if step["status"] == "failed"]
    completed = [step for step in state["evidence_results"] if step["status"] == "completed"]
    assert failed
    assert completed
    assert failed[0]["error"]


def test_deterministic_day_mismatch_forces_revision_even_if_model_passes():
    class _ThreeDayPlanner:
        def __init__(self):
            self.calls = 0

        async def __call__(self, request, context, previous_plan=None, feedback=None, on_token=None):
            self.calls += 1
            return "第一天：A\n第二天：B\n第三天：C"

    planner = _ThreeDayPlanner()
    state = _run(
        "上海2天预算500元",
        planner=planner,
        reflector=_Reflector([{"is_satisfactory": True}, {"is_satisfactory": True}]),
        executor=_Executor(),
        max_revisions=1,
    )

    assert planner.calls == 2
    assert state["status"] == "max_revisions_reached"
    assert any("计划天数不一致" in issue for issue in state["reflection"]["issues"])


def test_deterministic_budget_overrun_forces_revision():
    class _OverBudgetPlanner:
        async def __call__(self, request, context, previous_plan=None, feedback=None, on_token=None):
            return "第一天：A\n第二天：B\n总费用：1200元"

    state = _run(
        "上海2天预算800元",
        planner=_OverBudgetPlanner(),
        reflector=_Reflector([{"is_satisfactory": True}, {"is_satisfactory": True}]),
        executor=_Executor(),
        max_revisions=1,
    )

    assert state["status"] == "max_revisions_reached"
    assert any("预算超支" in issue for issue in state["reflection"]["issues"])


def test_deterministic_hard_error_skips_llm_reflection():
    class _CountingReflector:
        def __init__(self):
            self.calls = 0

        async def __call__(self, request, plan):
            self.calls += 1
            return {"is_satisfactory": True}

    class _OverBudgetPlanner:
        async def __call__(self, request, context, previous_plan=None, feedback=None, on_token=None):
            return "第一天：A\n总费用：1200元"

    reflector = _CountingReflector()
    state = _run(
        "上海2天预算800元",
        planner=_OverBudgetPlanner(),
        reflector=reflector,
        executor=_Executor(),
        max_revisions=0,
    )

    assert reflector.calls == 0
    assert state["reflection"]["source"] == "deterministic"
    assert any("预算超支" in issue for issue in state["reflection"]["issues"])


def test_single_clean_step_skips_llm_reflection():
    class _CountingReflector:
        def __init__(self):
            self.calls = 0

        async def __call__(self, request, plan):
            self.calls += 1
            return {"is_satisfactory": True}

    reflector = _CountingReflector()
    state = _run(
        "上海1天预算500元",
        planner=_Planner(),
        reflector=reflector,
        executor=_Executor(),
        max_revisions=1,
    )

    assert reflector.calls == 0
    assert state["reflection"]["source"] == "deterministic_fast_path"
    assert state["status"] == "approved"


def test_invalid_reflection_format_does_not_silently_pass():
    state = _run(
        "上海2天预算500元",
        planner=_Planner(),
        reflector=_Reflector(["pass", "看起来没问题"]),
        executor=_Executor(),
        max_revisions=1,
    )

    assert state["status"] == "max_revisions_reached"
    assert any(
        "审核结果" in issue or "缺少有效字段" in issue
        for issue in state["reflection"]["issues"]
    )


def test_travel_graph_records_model_and_execution_trace_spans():
    recorder = get_trace_recorder()
    recorder.clear()
    _run(
        "杭州3天预算1000元",
        planner=_Planner(),
        reflector=_Reflector([{"is_satisfactory": True}]),
        executor=_Executor(),
    )

    spans = recorder.summary()["spans"]
    assert spans["model.travel_plan"]["count"] == 1
    assert spans["travel.execute_evidence"]["count"] == 1
    assert spans["model.travel_reflect"]["count"] == 1
