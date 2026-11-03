"""通用任务规划子图：拆解 → 执行 → 校验 → 至多一次修订 → 收束。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.graphs.plan_models import GeneralPlanStep, GeneralTaskPlan
from app.llm_json import ainvoke_json
from app.graphs.context import TurnContext
from app.observability import timed_span
from app.tools.agent_capabilities import ToolAuthorizationError, validate_agent_tools


class GeneralPlanState(TypedDict, total=False):
    user_message: str
    user_id: str
    context: str
    plan: Dict[str, Any]
    revision_count: int
    validation: Dict[str, Any]
    response: str
    status: str
    trace_id: str


async def _planner(runtime: Runtime[TurnContext]):
    services = runtime.context.services
    override = runtime.context.extra.get("planner")
    if override is not None:
        return override
    llm = services.llm

    async def plan(request: str, context: str, previous_plan=None, feedback: str = "") -> GeneralTaskPlan:
        prompt = ChatPromptTemplate.from_messages([
            ("system", """把复杂任务拆成可执行步骤。每一步只做一件事。
如果步骤需要工具，使用 tool_name 和 JSON arguments；否则 tool_name 为 null。
不得虚构外部事实。输出结构化计划。"""),
            ("user", "任务：{request}\n\n上下文：{context}\n\n上一版：{previous}\n\n反馈：{feedback}"),
        ])
        return await ainvoke_json(llm, prompt, GeneralTaskPlan, {
            "request": request,
            "context": context,
            "previous": previous_plan.model_dump_json() if previous_plan else "无",
            "feedback": feedback or "无",
        })

    return plan


async def _executor(runtime: Runtime[TurnContext], tools_by_name: Dict[str, Any], policy: Any):
    override = runtime.context.extra.get("executor")
    if override is not None:
        return override

    async def execute(step: GeneralPlanStep) -> Dict[str, Any]:
        if not step.tool_name:
            return {"status": "completed", "result": f"已规划：{step.instruction}"}
        if policy is not None:
            try:
                policy.require(step.tool_name)
            except ToolAuthorizationError as exc:
                return {"status": "failed", "error": str(exc)}
        tool = tools_by_name.get(step.tool_name)
        if tool is None:
            return {"status": "failed", "error": f"未知工具: {step.tool_name}"}
        try:
            args = dict(step.arguments or {})
            if hasattr(tool, "ainvoke"):
                result = await tool.ainvoke(args)
            else:
                result = await asyncio.to_thread(tool.invoke, args)
            return {"status": "completed", "result": str(result)}
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    return execute


def _tooling(runtime: Runtime[TurnContext]):
    services = runtime.context.services
    actor_id = runtime.context.extra.get("actor_id") or ""
    agent_id = runtime.context.extra.get("agent_id") or "general"
    items = services.tools_for(actor_id, agent_id)
    policy = validate_agent_tools(agent_id, items) if runtime.context.extra.get("agent_id") else None
    return {item.name: item for item in items}, policy


async def make_plan(state: GeneralPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    planner = await _planner(runtime)
    with timed_span("model.general_plan", trace_id=state.get("trace_id", "")):
        plan = await planner(state["user_message"], state.get("context", ""))
    return {"plan": plan.model_dump(), "status": "planned"}


async def execute_steps(state: GeneralPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    tools_by_name, policy = _tooling(runtime)
    executor = await _executor(runtime, tools_by_name, policy)
    raw_steps = list((state.get("plan") or {}).get("steps") or [])
    steps = [GeneralPlanStep.model_validate(item) for item in raw_steps]
    results = await asyncio.gather(*(executor(step) for step in steps))
    for step, result in zip(steps, results):
        step.status = result.get("status", "completed")
        step.result = str(result.get("result") or "")
        step.error = str(result.get("error") or "")
    plan = dict(state.get("plan") or {})
    plan["steps"] = [step.model_dump() for step in steps]
    return {"plan": plan, "status": "executed"}


async def validate(state: GeneralPlanState) -> Dict[str, Any]:
    plan = GeneralTaskPlan.model_validate(state.get("plan") or {})
    issues: List[str] = []
    if not plan.steps:
        issues.append("计划没有可执行步骤")
    failed = [step for step in plan.steps if step.status == "failed"]
    if failed:
        issues.append(f"{len(failed)} 个步骤执行失败")
    return {"validation": {"is_satisfactory": not issues, "issues": issues}, "status": "validated"}


def should_continue(state: GeneralPlanState) -> str:
    if state.get("validation", {}).get("is_satisfactory"):
        return "finalize"
    return "revise" if state.get("revision_count", 0) < 1 else "finalize"


async def revise(state: GeneralPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    planner = await _planner(runtime)
    previous = GeneralTaskPlan.model_validate(state.get("plan") or {})
    feedback = "\n".join(state.get("validation", {}).get("issues") or [])
    plan = await planner(
        state["user_message"],
        state.get("context", ""),
        previous_plan=previous,
        feedback=feedback,
    )
    return {
        "plan": plan.model_dump(),
        "revision_count": state.get("revision_count", 0) + 1,
        "status": "revised",
    }


async def finalize(state: GeneralPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    plan = GeneralTaskPlan.model_validate(state.get("plan") or {})
    step_lines = [
        f"{index}. {step.title}: {step.result or step.instruction}"
        for index, step in enumerate(plan.steps, start=1)
    ]
    prompt = ChatPromptTemplate.from_messages([
        ("system", "根据计划和真实执行结果生成最终回答。失败步骤必须明确披露，不得编造。"),
        ("user", "任务：{request}\n\n计划与结果：\n{results}"),
    ])
    chain = prompt | runtime.context.services.llm
    response = ""
    async for chunk in chain.astream({
        "request": state["user_message"],
        "results": "\n".join(step_lines),
    }):
        if chunk.content:
            response += str(chunk.content)
            await sink.emit_token(str(chunk.content), step="generate")
    return {"response": response, "status": "completed"}


def build_general_plan_graph(checkpointer: Optional[Any] = None):
    graph = StateGraph(GeneralPlanState, context_schema=TurnContext)
    graph.add_node("make_plan", make_plan, timeout=120)
    graph.add_node("execute_steps", execute_steps, timeout=120)
    graph.add_node("validate", validate, timeout=10)
    graph.add_node("revise", revise, timeout=120)
    graph.add_node("finalize", finalize, timeout=120)
    graph.add_edge(START, "make_plan")
    graph.add_edge("make_plan", "execute_steps")
    graph.add_edge("execute_steps", "validate")
    graph.add_conditional_edges("validate", should_continue, {"revise": "revise", "finalize": "finalize"})
    graph.add_edge("revise", "execute_steps")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)
