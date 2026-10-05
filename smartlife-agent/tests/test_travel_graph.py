"""TravelPlanGraph 的步骤、执行、反思和修订闭环测试。"""

import asyncio

from app.agents.travel_graph import TravelPlanGraph


class _Planner:
    def __init__(self):
        self.calls = []

    async def __call__(self, request, context, previous_plan=None, feedback=None, on_token=None):
        self.calls.append((request, previous_plan, feedback))
        suffix = "修订版" if previous_plan else "初版"
        plan = f"{suffix}：\n第一天：西湖\n第二天：灵隐寺\n第三天：返程"
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


def _run(graph, request):
    return asyncio.run(graph.run_streaming(request, "user-1", "", thread_id="travel:test"))


def test_travel_graph_executes_steps_and_revises_until_approved():
    planner = _Planner()
    executor = _Executor()
    graph = TravelPlanGraph(
        planner=planner,
        reflector=_Reflector([
            {"is_satisfactory": False, "suggestions": ["增加休息时间"]},
            {"is_satisfactory": True, "suggestions": []},
        ]),
        executor=executor,
        max_revisions=3,
    )

    state = asyncio.run(graph.run_streaming(
        "杭州3天预算1000元",
        "user-1",
        "",
        thread_id="travel:test",
        chat_history=[{"role": "user", "content": "从上海出发"}],
    ))

    assert state["requirements"]["destination"] == "杭州"
    assert state["requirements"]["days"] == 3
    assert state["chat_history"] == [{"role": "user", "content": "从上海出发"}]
    assert state["revision_count"] == 1
    assert len(state["plan_versions"]) == 2
    assert len(state["steps"]) == 3
    assert [step["status"] for step in state["steps"]] == ["completed"] * 3
    assert state["status"] == "approved"
    assert len(planner.calls) == 2
    assert len(executor.calls) == 6


def test_travel_graph_stops_at_max_revisions_with_best_effort_status():
    graph = TravelPlanGraph(
        planner=_Planner(),
        reflector=_Reflector([
            {"is_satisfactory": False, "suggestions": ["仍需修改"]},
            {"is_satisfactory": False, "suggestions": ["仍需修改"]},
        ]),
        executor=_Executor(),
        max_revisions=1,
    )

    state = _run(graph, "北京2天预算800元")

    assert state["revision_count"] == 1
    assert len(state["plan_versions"]) == 2
    assert state["status"] == "max_revisions_reached"


def test_travel_graph_requires_clarification_for_missing_core_fields():
    graph = TravelPlanGraph(
        planner=_Planner(),
        reflector=_Reflector([{"is_satisfactory": True}]),
        executor=_Executor(),
    )

    state = _run(graph, "帮我规划一次旅行")

    assert state["status"] == "needs_clarification"
    assert "目的地" in state["final_plan"]
    assert "天数" in state["final_plan"]
    assert state["plan_versions"] == []


def test_travel_graph_records_executor_failure_without_crashing():
    graph = TravelPlanGraph(
        planner=_Planner(),
        reflector=_Reflector([
            {"is_satisfactory": False, "suggestions": ["修复执行问题"]},
            {"is_satisfactory": True, "suggestions": []},
        ]),
        executor=_Executor(fail_first=True),
        max_revisions=3,
    )

    state = _run(graph, "上海2天预算500元")

    failed = [step for step in state["step_results"] if step["status"] == "failed"]
    completed = [step for step in state["step_results"] if step["status"] == "completed"]
    assert failed
    assert completed
    assert failed[0]["error"]
