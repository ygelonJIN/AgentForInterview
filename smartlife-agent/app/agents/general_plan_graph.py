"""通用 Plan-Execute 子图：结构化计划、工具执行、硬校验和一次修订。"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.observability import timed_span


async def _resolve(value):
    return await value if inspect.isawaitable(value) else value


class GeneralPlanStep(BaseModel):
    title: str
    instruction: str
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    status: str = "pending"
    error: str = ""


class GeneralTaskPlan(BaseModel):
    summary: str = ""
    steps: List[GeneralPlanStep] = Field(default_factory=list)
    budget: Optional[float] = Field(default=None, ge=0)
    constraints: List[str] = Field(default_factory=list)


class GeneralPlanState(TypedDict, total=False):
    request: str
    context: str
    plan: Dict[str, Any]
    revision_count: int
    validation: Dict[str, Any]
    response: str
    status: str
    trace_id: str


class GeneralPlanGraph:
    """用于非购物、非旅行的复杂多步任务。"""

    def __init__(
        self,
        llm,
        *,
        tools: Sequence[Any] = (),
        planner: Optional[Callable[..., Awaitable[GeneralTaskPlan]]] = None,
        executor: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
        max_revisions: int = 1,
    ):
        self.llm = llm
        self.tools_by_name = {item.name: item for item in tools}
        self.planner = planner or self._default_planner
        self.executor = executor or self._execute_step
        self.max_revisions = max_revisions
        self.graph = self._build_graph()

    async def _default_planner(
        self,
        request: str,
        context: str,
        previous_plan: Optional[GeneralTaskPlan] = None,
        feedback: str = "",
    ) -> GeneralTaskPlan:
        prompt = ChatPromptTemplate.from_messages([
            ("system", """把复杂任务拆成可执行步骤。每一步只做一件事。
如果步骤需要工具，使用 tool_name 和 JSON arguments；否则 tool_name 为 null。
不得虚构外部事实。输出结构化计划。"""),
            ("user", "任务：{request}\n\n上下文：{context}\n\n上一版：{previous}\n\n反馈：{feedback}"),
        ])
        chain = prompt | self.llm.with_structured_output(GeneralTaskPlan)
        return await chain.ainvoke({
            "request": request,
            "context": context,
            "previous": previous_plan.model_dump_json() if previous_plan else "无",
            "feedback": feedback or "无",
        })

    async def _execute_step(self, step: GeneralPlanStep) -> Dict[str, Any]:
        if not step.tool_name:
            return {"status": "completed", "result": f"已规划：{step.instruction}"}
        tool = self.tools_by_name.get(step.tool_name)
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

    def _build_graph(self):
        graph = StateGraph(GeneralPlanState)
        graph.add_node("make_plan", self._make_plan)
        graph.add_node("execute_steps", self._execute_steps)
        graph.add_node("validate", self._validate)
        graph.add_node("revise", self._revise)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "make_plan")
        graph.add_edge("make_plan", "execute_steps")
        graph.add_edge("execute_steps", "validate")
        graph.add_conditional_edges(
            "validate",
            self._should_continue,
            {"revise": "revise", "finalize": "finalize"},
        )
        graph.add_edge("revise", "execute_steps")
        graph.add_edge("finalize", END)
        return graph.compile()

    async def _make_plan(self, state: GeneralPlanState) -> Dict[str, Any]:
        with timed_span("model.general_plan", trace_id=state.get("trace_id", "")):
            plan = await _resolve(self.planner(state["request"], state["context"]))
        return {"plan": plan.model_dump(), "status": "planned"}

    async def _execute_steps(self, state: GeneralPlanState) -> Dict[str, Any]:
        raw_steps = list((state.get("plan") or {}).get("steps") or [])
        steps = [GeneralPlanStep.model_validate(item) for item in raw_steps]
        results = await asyncio.gather(*(self.executor(step) for step in steps))
        for step, result in zip(steps, results):
            step.status = result.get("status", "completed")
            step.result = str(result.get("result") or "")
            step.error = str(result.get("error") or "")
        plan = dict(state.get("plan") or {})
        plan["steps"] = [step.model_dump() for step in steps]
        return {"plan": plan, "status": "executed"}

    @staticmethod
    def _validate(state: GeneralPlanState) -> Dict[str, Any]:
        plan = GeneralTaskPlan.model_validate(state.get("plan") or {})
        issues = []
        if not plan.steps:
            issues.append("计划没有可执行步骤")
        failed = [step for step in plan.steps if step.status == "failed"]
        if failed:
            issues.append(f"{len(failed)} 个步骤执行失败")
        return {"validation": {"is_satisfactory": not issues, "issues": issues}, "status": "validated"}

    def _should_continue(self, state: GeneralPlanState) -> str:
        if state.get("validation", {}).get("is_satisfactory"):
            return "finalize"
        return "revise" if state.get("revision_count", 0) < self.max_revisions else "finalize"

    async def _revise(self, state: GeneralPlanState) -> Dict[str, Any]:
        previous = GeneralTaskPlan.model_validate(state.get("plan") or {})
        feedback = "\n".join(state.get("validation", {}).get("issues") or [])
        plan = await _resolve(self.planner(
            state["request"],
            state["context"],
            previous_plan=previous,
            feedback=feedback,
        ))
        return {
            "plan": plan.model_dump(),
            "revision_count": state.get("revision_count", 0) + 1,
            "status": "revised",
        }

    async def _finalize(self, state: GeneralPlanState) -> Dict[str, Any]:
        plan = GeneralTaskPlan.model_validate(state.get("plan") or {})
        step_lines = [
            f"{index}. {step.title}: {step.result or step.instruction}"
            for index, step in enumerate(plan.steps, start=1)
        ]
        prompt = ChatPromptTemplate.from_messages([
            ("system", "根据计划和真实执行结果生成最终回答。失败步骤必须明确披露，不得编造。"),
            ("user", "任务：{request}\n\n计划与结果：\n{results}"),
        ])
        chain = prompt | self.llm
        response = ""
        async for chunk in chain.astream({"request": state["request"], "results": "\n".join(step_lines)}):
            if chunk.content:
                response += str(chunk.content)
        return {"response": response, "status": "completed"}

    async def run_streaming(
        self,
        request: str,
        *,
        context: str = "",
        queue=None,
        trace_id: str = "",
    ) -> str:
        result = await self.graph.ainvoke({
            "request": request,
            "context": context,
            "plan": {},
            "revision_count": 0,
            "validation": {},
            "status": "started",
            "trace_id": trace_id,
        })
        response = result.get("response", "")
        if queue and response:
            await queue.emit_token(response, step="generate")
        return response
