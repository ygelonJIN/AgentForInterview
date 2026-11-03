"""旅行子图：上下文准备 → 需求抽取 → 证据采集 → 计划 → 硬错误审核 → 修订 → 收束。

纯 LangGraph 版本：不再有回调对象和 ``on_event`` 翻译层，
每个节点自己写 custom 事件；依赖（planner / reflector / executor / 上下文构建）
从 ``runtime.context.services.travel_service`` 取，测试可用 ``context.extra`` 覆盖。
"""
from __future__ import annotations

import inspect
import json
import re
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from app.execution_log import build_execution_log
from app.graphs._util import resolve
from app.graphs.context import TurnContext
from app.observability import timed_span
from app.reliability import RetryableError
from app.streaming import EventType

MAX_REVISIONS = 3

PLAN_RETRY = RetryPolicy(
    max_attempts=2,
    initial_interval=0.05,
    retry_on=(RetryableError, ConnectionError, TimeoutError),
)

DAY_PATTERN = re.compile(
    r"(?:^|\n)((?:第\s*[一二三四五六七八九十\d]+\s*天|Day\s*\d+)[^\n]*)",
    re.IGNORECASE,
)


class TravelPlanState(TypedDict, total=False):
    # 输入（父图透传）
    user_message: str
    original_request: str
    user_id: str
    thread_id: str
    classification: Dict[str, Any]
    constraints: Dict[str, Any]
    constraints_text: str

    # 上下文
    context: str
    chat_history: List[Any]
    max_revisions: int
    step3_emitted: bool

    # 需求与证据
    requirements: Dict[str, Any]
    clarifications: List[str]
    evidence_steps: List[Dict[str, Any]]
    evidence_results: List[Dict[str, Any]]
    evidence_context: str

    # 计划与审核
    current_plan: str
    plan_versions: List[str]
    reflection: Dict[str, Any]
    revision_count: int
    final_plan: str

    # 输出
    response: str
    status: str
    trace_id: str


# ---------------------------------------------------------------- 基础设施


def _service(runtime: Runtime[TurnContext]):
    return runtime.context.services.travel_service


def _planner(runtime: Runtime[TurnContext]):
    override = runtime.context.extra.get("planner")
    if override is not None:
        return override
    service = _service(runtime)
    trace_id = runtime.context.trace_id
    request_llm = runtime.context.services.llm
    reflect_llm = getattr(service, "reflect_llm", None) or request_llm

    async def planner(request, context, previous_plan=None, feedback=None, on_token=None):
        return await service._planner_streaming(
            request,
            context,
            previous_plan=previous_plan,
            feedback=feedback,
            on_token=on_token,
            llm=reflect_llm if previous_plan else request_llm,
            trace_id=trace_id,
        )

    return planner


def _reflector(runtime: Runtime[TurnContext]):
    override = runtime.context.extra.get("reflector")
    if override is not None:
        return override
    service = _service(runtime)
    trace_id = runtime.context.trace_id
    reflect_llm = getattr(service, "reflect_llm", None) or runtime.context.services.llm

    async def reflector(request, plan):
        return await service._reflector_streaming(
            request, plan, llm=reflect_llm, trace_id=trace_id
        )

    return reflector


def _executor(runtime: Runtime[TurnContext]):
    override = runtime.context.extra.get("executor")
    if override is not None:
        return override

    async def default_executor(step: Dict[str, Any], _state: TravelPlanState) -> Dict[str, Any]:
        return {
            "status": "completed",
            "result": "已结合当前上下文和本地攻略执行；天气和路线通过真实 provider 查询",
            "source": "local_context",
        }

    service = _service(runtime)
    runner = getattr(service, "_execute_plan_step", None)
    return runner or default_executor


async def _log(sink, node: str, kind: str, message: str, **kwargs: Any) -> None:
    payload = build_execution_log(
        graph="travel", node=node, kind=kind, message=message, **kwargs
    )
    await sink.emit_execution_log(payload)


async def _emit_step3(sink, state: TravelPlanState, description: str, status: str) -> bool:
    """步骤 3 只广播一次，后续阶段变化用 token/reset 表达。"""
    if state.get("step3_emitted"):
        return True
    await sink.emit(EventType.STEP, {
        "step": 3, "total": 4,
        "name": "生成并审核回复",
        "description": description,
        "status": status,
    }, step="generate")
    return True


def extract_requirements(request: str) -> Dict[str, Any]:
    text = request or ""
    destinations = ["杭州", "北京", "上海", "成都", "三亚", "西安", "丽江", "大理"]
    destination = next((item for item in destinations if item in text), None)
    day_match = re.search(r"(\d+)\s*(?:天|日)", text)
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|¥)", text)
    people_match = re.search(r"(\d+)\s*人", text)
    return {
        "destination": destination,
        "days": int(day_match.group(1)) if day_match else None,
        "budget": float(budget_match.group(1)) if budget_match else None,
        "people": int(people_match.group(1)) if people_match else 1,
        "start_date": date_match.group(1) if date_match else None,
    }


def normalize_reflection(value: Any) -> Dict[str, Any]:
    if hasattr(value, "model_dump"):
        result = value.model_dump()
    elif isinstance(value, dict):
        result = value
    else:
        content = str(value or "")
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            result = {}
            if json_match:
                try:
                    result = json.loads(json_match.group(0))
                except json.JSONDecodeError:
                    result = {}
            if not result:
                raw_issue = content.strip() or "计划审核结果为空"
                issue = f"计划审核结果无效：{raw_issue}"
                return {
                    "is_satisfactory": False,
                    "issues": [issue],
                    "suggestions": [issue],
                    "severity": "critical",
                }
    recognized = {"severity", "is_satisfactory", "issues", "suggestions"}
    if isinstance(result, dict) and not recognized.intersection(result):
        return {
            "is_satisfactory": False,
            "issues": ["计划审核结果缺少有效字段"],
            "suggestions": ["请重新输出包含 severity、is_satisfactory、issues、suggestions 的 JSON"],
        }
    issues = [str(item) for item in result.get("issues", []) if str(item).strip()]
    severity = str(result.get("severity", "")).lower()
    requested_revision = result.get("is_satisfactory") is False
    critical = requested_revision or severity == "critical" or bool(re.search(
        r"预算超支|天数(?:不符|不一致)|路线(?:冲突|不合理)|无法执行|必须修改|需要修订",
        "\n".join(issues),
    ))
    return {
        "is_satisfactory": not critical,
        "issues": issues,
        "suggestions": [str(item) for item in result.get("suggestions", []) if str(item).strip()],
        "severity": "critical" if critical else severity or "pass",
    }


def deterministic_plan_issues(state: TravelPlanState) -> List[str]:
    """独立于模型审核的硬约束校验。"""
    issues: List[str] = []
    plan = str(state.get("current_plan") or "").strip()
    if not plan:
        return ["计划正文为空"]
    requirements = state.get("requirements") or {}
    requested_days = requirements.get("days")
    plan_days = len(DAY_PATTERN.findall(plan))
    if requested_days and plan_days and plan_days != int(requested_days):
        issues.append(f"计划天数不一致：要求 {requested_days} 天，实际生成 {plan_days} 天")
    requested_budget = requirements.get("budget")
    total_match = re.search(
        r"(?:总预算|总费用|总花费|总计)\s*(?:为|是|:|：)?\s*¥?\s*(\d+(?:\.\d+)?)",
        plan,
    )
    if requested_budget and total_match:
        total_cost = float(total_match.group(1))
        if total_cost > float(requested_budget) * 1.05:
            issues.append(f"预算超支：要求 {requested_budget} 元，计划总计 {total_cost:g} 元")
    failed_steps = [
        step for step in state.get("evidence_results") or []
        if step.get("status") != "completed"
    ]
    if failed_steps:
        issues.append(f"计划步骤执行失败：{len(failed_steps)} 个步骤未完成")
    return issues


def can_use_deterministic_reflection(state: TravelPlanState) -> bool:
    steps = state.get("evidence_steps") or []
    step_results = state.get("evidence_results") or []
    if len(steps) != 1 or len(step_results) != 1:
        return False
    result = step_results[0]
    if result.get("status") != "completed" or result.get("error"):
        return False
    if result.get("warnings") or result.get("simulated"):
        return False
    return all(
        tool.get("ok") and not (tool.get("result") or {}).get("stale")
        for tool in result.get("tool_calls") or []
    )


def route_pair(text: str):
    """从「从 A 到 B」里抽路线端点；抽不到返回 None。"""
    match = re.search(r"从\s*([^\s，,。]{2,10})\s*到\s*([^\s，,。]{2,10})", text or "")
    return (match.group(1), match.group(2)) if match else None


# ---------------------------------------------------------------- 节点


async def build_context(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    ctx = runtime.context
    sink = ctx.sink_for(runtime)
    request = state.get("original_request") or state.get("user_message", "")
    await sink.emit(EventType.STEP, {
        "step": 2, "total": 4,
        "name": "资料检索与准备",
        "description": "正在读取本地攻略、当前需求和长期跨场景 MD 记忆",
        "status": "active",
    }, step="retrieval")

    constraints_text = state.get("constraints_text", "")
    if not constraints_text and state.get("constraints"):
        from app.services.task_models import SharedConstraints

        try:
            constraints_text = SharedConstraints.model_validate(state["constraints"]).prompt_context()
        except Exception:
            constraints_text = ""

    chat_history = ctx.extra.get("chat_history")
    if chat_history is None:
        chat_history = ctx.services.get_chat_history(state.get("thread_id", ""))
    chat_history = list(chat_history or [])

    builder = ctx.extra.get("context_builder")
    if builder is not None:
        context = builder(request)
    else:
        service = _service(runtime)
        context = service._build_plan_context(
            request,
            state.get("user_id", ""),
            chat_history,
            constraints=constraints_text,
        )
    return {
        "original_request": request,
        "context": context,
        "constraints_text": constraints_text,
        "chat_history": chat_history,
        "max_revisions": int(ctx.extra.get("max_revisions", MAX_REVISIONS)),
        "revision_count": 0,
        "plan_versions": [],
        "status": "context_ready",
    }


async def extract_requirements_node(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    requirements = extract_requirements(state.get("original_request") or state.get("user_message", ""))
    clarifications = [
        name for name, key in (("目的地", "destination"), ("天数", "days"))
        if requirements.get(key) is None
    ]
    await sink.emit("requirements", {"requirements": requirements, "clarifications": clarifications}, step="plan")
    await _log(
        sink,
        "extract_requirements",
        "branch_selected",
        "缺少核心字段，进入澄清分支" if clarifications else "核心字段完整，进入证据采集分支",
        status="warning" if clarifications else "ok",
        step="plan",
        branch="clarify" if clarifications else "build_steps",
        details={"requirements": requirements, "clarifications": clarifications},
    )
    return {
        "requirements": requirements,
        "clarifications": clarifications,
        "status": "needs_clarification" if clarifications else "ready",
    }


def should_plan(state: TravelPlanState) -> str:
    return "clarify" if state.get("clarifications") else "build_steps"


async def clarify(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    missing = "、".join(state.get("clarifications", []))
    message = f"为了生成可靠行程，请先补充：{missing}。"
    await sink.emit("clarification", {"message": message}, step="clarify")
    await sink.emit_token(message, step="clarify")
    return {
        "final_plan": message,
        "response": message,
        "current_plan": "",
        "status": "needs_clarification",
    }


async def build_steps(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    requirements = state.get("requirements") or {}
    destination = str(requirements.get("destination") or "").strip()
    request = state.get("original_request") or ""
    evidence_steps = [{
        "id": "evidence-weather",
        "title": "天气证据",
        "description": f"{destination} 天气查询",
        "status": "pending",
        "result": "",
        "error": "",
    }]
    if requirements.get("days") != 1:
        evidence_steps.append({
            "id": "evidence-activities",
            "title": "本地活动证据",
            "description": f"{destination} 本地活动资料",
            "status": "pending",
            "result": "",
            "error": "",
        })
    pair = route_pair(request)
    if pair:
        origin, target = pair
        evidence_steps.append({
            "id": "evidence-route",
            "title": "路线证据",
            "description": f"从{origin}到{target}",
            "status": "pending",
            "result": "",
            "error": "",
        })
    await sink.emit("steps_built", {"count": len(evidence_steps), "kind": "evidence"}, step="plan")
    emitted = await _emit_step3(
        sink, state, "只核对预算总额、天数、路线和明显无法执行的硬错误", "active"
    )
    return {"evidence_steps": evidence_steps, "status": "evidence_planned", "step3_emitted": emitted}


async def execute_steps(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    executor = _executor(runtime)
    all_steps = state.get("evidence_steps", [])
    executed = []
    with timed_span(
        "travel.execute_evidence",
        trace_id=state.get("trace_id", ""),
        attributes={"step_count": len(all_steps)},
    ):
        for index, step in enumerate(all_steps, start=1):
            await _log(
                sink, "execute_steps", "loop_iteration",
                f"执行证据步骤 {index}/{len(all_steps)}",
                status="running", step="execute",
                iteration=index, max_iterations=len(all_steps),
                details={"step_id": step.get("id", ""), "revision": state.get("revision_count", 0)},
            )
            item = dict(step)
            try:
                result = await resolve(executor(item, state))
                if not isinstance(result, dict):
                    result = {"status": "completed", "result": str(result), "source": "executor"}
                item.update({
                    "status": result.get("status", "completed"),
                    "result": result.get("result", ""),
                    "error": result.get("error", ""),
                    "source": result.get("source", "executor"),
                    "tool_calls": result.get("tool_calls", []),
                    "simulated": result.get("simulated", False),
                    "warnings": result.get("warnings", []),
                })
            except Exception as exc:
                item.update({
                    "status": "failed",
                    "result": "",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
            for tool_call in item.get("tool_calls", []):
                tool_result = tool_call.get("result") or {}
                fallback = bool(
                    tool_call.get("ok") is False
                    or tool_result.get("stale")
                    or tool_result.get("cache_status") == "stale_fallback"
                    or tool_result.get("simulated")
                )
                await _log(
                    sink, "execute_steps",
                    "tool_fallback" if fallback else "tool_completed",
                    f"{tool_call.get('tool', '工具')} 数据" + (
                        f"不可用：{str(tool_result.get('error') or '')[:120]}" if fallback else "执行成功"
                    ),
                    status="warning" if fallback else "ok",
                    step="execute",
                    iteration=index, max_iterations=len(all_steps),
                    details={
                        "step_id": item.get("id", ""),
                        "tool": tool_call.get("tool", ""),
                        "cache_status": tool_result.get("cache_status"),
                        "stale": tool_result.get("stale"),
                        "simulated": tool_result.get("simulated"),
                        "error": tool_result.get("error", ""),
                        "warnings": tool_result.get("warnings", []),
                    },
                )
            executed.append(item)
    evidence_parts = [
        f"【{item.get('title') or item.get('id')}】\n{item.get('result') or item.get('error') or '无可用证据'}"
        for item in executed
    ]
    return {
        "evidence_results": executed,
        "evidence_context": "\n\n".join(evidence_parts),
        "status": "evidence_executed",
    }


def _planner_context(state: TravelPlanState) -> str:
    parts = [state.get("context") or ""]
    if state.get("evidence_context"):
        parts.append("【真实工具证据】\n" + state["evidence_context"])
    return "\n\n".join(part for part in parts if part.strip())


async def make_plan(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    planner = _planner(runtime)
    revision_count = state.get("revision_count", 0)
    await sink.emit("plan_started", {"revision": revision_count}, step="plan")

    async def on_token(token: str, stage: str):
        await sink.emit_token(token, step=stage)

    emitted = await _emit_step3(
        sink, state,
        f"发现严重硬错误，正在修订（第 {revision_count + 1}/{state.get('max_revisions', MAX_REVISIONS)} 次）"
        if revision_count else "正在生成完整行程，并准备宽松硬错误检查",
        "active",
    )
    feedback = "\n".join(state.get("reflection", {}).get("suggestions") or [])
    kwargs = {
        "previous_plan": state.get("current_plan") if revision_count else None,
        "feedback": feedback if revision_count else None,
    }
    if "on_token" in inspect.signature(planner).parameters:
        kwargs["on_token"] = on_token
    with timed_span(
        "model.travel_plan",
        trace_id=state.get("trace_id", ""),
        attributes={"revision": revision_count},
    ):
        plan = await resolve(planner(
            state["original_request"],
            _planner_context(state),
            **kwargs,
        ))
    return {
        "current_plan": plan,
        "plan_versions": list(state.get("plan_versions") or []) + [plan],
        "step3_emitted": emitted,
        "status": "planned",
    }


async def reflect(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    issues = deterministic_plan_issues(state)
    if issues:
        reflection = {
            "is_satisfactory": False,
            "severity": "critical",
            "issues": list(issues),
            "suggestions": list(issues),
            "source": "deterministic",
        }
    elif can_use_deterministic_reflection(state):
        reflection = {
            "is_satisfactory": True,
            "severity": "pass",
            "issues": [],
            "suggestions": [],
            "source": "deterministic_fast_path",
        }
    else:
        reflector = _reflector(runtime)
        with timed_span(
            "model.travel_reflect",
            trace_id=state.get("trace_id", ""),
            attributes={"revision": state.get("revision_count", 0)},
        ):
            reflection = normalize_reflection(await resolve(reflector(
                state["original_request"],
                state["current_plan"],
            )))
    await sink.emit("reflection", reflection, step="plan")
    await _log(
        sink, "reflect", "decision",
        "审核通过" if reflection.get("is_satisfactory") else f"发现硬错误：{reflection.get('severity')}",
        status="ok" if reflection.get("is_satisfactory") else "warning",
        step="reflect",
        branch="finalize" if reflection.get("is_satisfactory") else "revise_or_limit",
        details={"severity": reflection.get("severity"), "issues": reflection.get("issues", [])},
    )
    return {"reflection": reflection, "status": "reflected"}


def should_continue(state: TravelPlanState) -> str:
    reflection = state.get("reflection", {})
    if reflection.get("is_satisfactory", True):
        return "finalize"
    if state.get("revision_count", 0) >= state.get("max_revisions", MAX_REVISIONS):
        return "finalize"
    return "revise"


async def revise(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    feedback = "\n".join(state.get("reflection", {}).get("suggestions") or [])
    max_revisions = state.get("max_revisions", MAX_REVISIONS)
    revision = state.get("revision_count", 0) + 1
    await sink.emit("revision", {"revision_count": revision, "feedback": feedback}, step="revise")
    await sink.emit(EventType.RESPONSE_RESET, {}, step="revise")
    await _log(
        sink, "revise", "loop_iteration",
        f"进入第 {revision}/{max_revisions} 次修订循环",
        status="warning", step="revise",
        iteration=revision, max_iterations=max_revisions,
        details={"feedback": feedback},
    )
    return {"revision_count": revision, "status": "revise_requested"}


async def finalize(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    sink = runtime.context.sink_for(runtime)
    satisfactory = state.get("reflection", {}).get("is_satisfactory", True)
    status = "approved" if satisfactory else "max_revisions_reached"
    plan = state.get("current_plan", "") or ""
    strip_notes = getattr(runtime.context.services.travel_service, "_remove_revision_notes", None)
    response = strip_notes(plan) if strip_notes else plan
    await sink.emit("finalized", {"status": status, "revision_count": state.get("revision_count", 0)}, step="plan")
    await _log(
        sink, "finalize",
        "limit_reached" if status == "max_revisions_reached" else "completed",
        (
            f"修订达到上限 {state.get('max_revisions', MAX_REVISIONS)} 次，保留当前版本"
            if status == "max_revisions_reached"
            else f"旅行计划完成，修订 {state.get('revision_count', 0)} 次"
        ),
        status="warning" if status == "max_revisions_reached" else "ok",
        step="plan",
        iteration=state.get("revision_count", 0),
        max_iterations=state.get("max_revisions", MAX_REVISIONS),
        details={"status": status},
    )
    return {"final_plan": response, "response": response, "status": status}


def build_travel_plan_graph(checkpointer: Optional[Any] = None):
    graph = StateGraph(TravelPlanState, context_schema=TurnContext)
    graph.add_node("build_context", build_context, timeout=30)
    graph.add_node("extract_requirements", extract_requirements_node, timeout=5)
    graph.add_node("clarify", clarify, timeout=5)
    graph.add_node("build_steps", build_steps, timeout=5)
    graph.add_node("execute_steps", execute_steps, timeout=120)
    graph.add_node("make_plan", make_plan, retry_policy=PLAN_RETRY, timeout=120)
    graph.add_node("reflect", reflect, timeout=90)
    graph.add_node("revise", revise, timeout=120)
    graph.add_node("finalize", finalize, timeout=5)
    graph.add_edge(START, "build_context")
    graph.add_edge("build_context", "extract_requirements")
    graph.add_conditional_edges(
        "extract_requirements", should_plan,
        {"clarify": "clarify", "build_steps": "build_steps"},
    )
    graph.add_edge("clarify", END)
    graph.add_edge("build_steps", "execute_steps")
    graph.add_edge("execute_steps", "make_plan")
    graph.add_edge("make_plan", "reflect")
    graph.add_conditional_edges("reflect", should_continue, {"revise": "revise", "finalize": "finalize"})
    graph.add_edge("revise", "make_plan")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------- 路由（轻量对话分支）


async def travel_chat(state: TravelPlanState, runtime: Runtime[TurnContext]) -> Dict[str, Any]:
    ctx = runtime.context
    response = await ctx.services.travel_service.chat_streaming(
        state.get("user_message", ""),
        state.get("user_id", ""),
        ctx.sink_for(runtime),
    )
    return {"response": response, "status": "generated"}


def build_travel_graph(checkpointer: Optional[Any] = None):
    """按 route 在「计划执行」与「轻量对话」之间分支；两条边都汇到 END。"""
    graph = StateGraph(TravelPlanState, context_schema=TurnContext)
    plan_graph = build_travel_plan_graph()
    graph.add_node("travel_plan", plan_graph)
    graph.add_node("travel_chat", travel_chat, timeout=120)
    graph.add_conditional_edges(
        START,
        lambda state: "travel_plan" if (state.get("classification") or {}).get("route") == "plan_and_execute" else "travel_chat",
        {"travel_plan": "travel_plan", "travel_chat": "travel_chat"},
    )
    graph.add_edge("travel_plan", END)
    graph.add_edge("travel_chat", END)
    return graph.compile(checkpointer=checkpointer)
