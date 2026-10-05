"""
旅游 Plan & Execute 子图。

state 明确保留需求、计划版本、结构化步骤、执行结果、反思和修订次数，
同步/流式入口共享同一套状态转移。
"""
import asyncio
import inspect
import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph


class TravelPlanState(TypedDict):
    original_request: str
    user_id: str
    chat_history: List[Any]
    context: str
    requirements: Dict[str, Any]
    clarifications: List[str]
    plan_versions: List[str]
    current_plan: str
    steps: List[Dict[str, Any]]
    step_results: List[Dict[str, Any]]
    reflection: Dict[str, Any]
    revision_count: int
    final_plan: str
    status: str


async def _resolve(value):
    if inspect.isawaitable(value):
        return await value
    return value


class TravelPlanGraph:
    """以 LangGraph 表达的旅行计划、执行、反思和修订闭环。"""

    def __init__(
        self,
        planner: Callable[..., Awaitable[str]],
        reflector: Callable[..., Awaitable[Any]],
        executor: Optional[Callable[..., Any]] = None,
        on_event: Optional[Callable[..., Any]] = None,
        on_token: Optional[Callable[..., Any]] = None,
        max_revisions: int = 3,
    ):
        if max_revisions < 0:
            raise ValueError("max_revisions 不能为负数")
        self.planner = planner
        self.reflector = reflector
        self.executor = executor or self._default_executor
        self.on_event = on_event
        self.on_token = on_token
        self.max_revisions = max_revisions
        self.graph = self._build_graph()

    @staticmethod
    async def _default_executor(step: Dict[str, Any], _state: TravelPlanState) -> Dict[str, Any]:
        return {
            "status": "completed",
            "result": "已结合当前上下文和本地攻略执行；天气、路线和酒店可通过配置的外部 provider 查询",
            "source": "local_context",
        }

    async def _emit(self, event: str, data: Dict[str, Any]):
        if self.on_event:
            await _resolve(self.on_event(event, data))

    @staticmethod
    def _extract_requirements(request: str) -> Dict[str, Any]:
        text = request or ""
        destinations = ["杭州", "北京", "上海", "成都", "三亚", "西安", "丽江", "大理"]
        destination = next((item for item in destinations if item in text), None)
        day_match = re.search(r"(\d+)\s*(?:天|日)", text)
        budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|¥)", text)
        people_match = re.search(r"(\d+)\s*人", text)
        return {
            "destination": destination,
            "days": int(day_match.group(1)) if day_match else None,
            "budget": float(budget_match.group(1)) if budget_match else None,
            "people": int(people_match.group(1)) if people_match else 1,
        }

    @staticmethod
    def _parse_steps(plan: str) -> List[Dict[str, Any]]:
        text = plan or ""
        pattern = re.compile(
            r"(?:^|\n)((?:第\s*[一二三四五六七八九十\d]+\s*天|Day\s*\d+)[^\n]*)",
            re.IGNORECASE,
        )
        matches = list(pattern.finditer(text))
        steps: List[Dict[str, Any]] = []
        if matches:
            for index, match in enumerate(matches):
                start = match.start(1)
                end = matches[index + 1].start(1) if index + 1 < len(matches) else len(text)
                steps.append({
                    "id": f"day-{index + 1}",
                    "title": match.group(1).strip(),
                    "description": text[start:end].strip(),
                    "status": "pending",
                    "result": "",
                    "error": "",
                })
        else:
            steps.append({
                "id": "plan-1",
                "title": "完整行程",
                "description": text.strip(),
                "status": "pending",
                "result": "",
                "error": "",
            })
        return steps

    @staticmethod
    def _normalize_reflection(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            result = value
        else:
            content = str(value or "")
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", content, re.DOTALL)
                if json_match:
                    try:
                        result = json.loads(json_match.group(0))
                    except json.JSONDecodeError:
                        result = {}
                else:
                    result = {}
                if not result:
                    has_hard_error = bool(re.search(
                        r"预算超支|天数(?:不符|不一致)|路线(?:冲突|不合理)|无法执行|必须修改|需要修订",
                        content,
                    ))
                    return {
                        "is_satisfactory": not has_hard_error,
                        "issues": [content] if has_hard_error else [],
                        "suggestions": [content] if has_hard_error else [],
                    }
        issues = [str(item) for item in result.get("issues", []) if str(item).strip()]
        severity = str(result.get("severity", "")).lower()
        critical = severity == "critical" or bool(re.search(
            r"预算超支|天数(?:不符|不一致)|路线(?:冲突|不合理)|无法执行|必须修改|需要修订",
            "\n".join(issues),
        ))
        return {
            "is_satisfactory": not critical,
            "issues": issues,
            "suggestions": [
                str(item) for item in result.get("suggestions", []) if str(item).strip()
            ],
            "severity": "critical" if critical else severity or "pass",
        }

    async def _extract(self, state: TravelPlanState) -> Dict[str, Any]:
        requirements = self._extract_requirements(state["original_request"])
        clarifications = [
            name for name, key in (("目的地", "destination"), ("天数", "days"))
            if requirements.get(key) is None
        ]
        status = "needs_clarification" if clarifications else "ready"
        await self._emit("requirements", {"requirements": requirements, "clarifications": clarifications})
        return {"requirements": requirements, "clarifications": clarifications, "status": status}

    def _should_plan(self, state: TravelPlanState) -> str:
        return "clarify" if state.get("clarifications") else "make_plan"

    async def _clarify(self, state: TravelPlanState) -> Dict[str, Any]:
        missing = "、".join(state.get("clarifications", []))
        message = f"为了生成可靠行程，请先补充：{missing}。"
        await self._emit("clarification", {"message": message})
        return {"final_plan": message, "current_plan": "", "status": "needs_clarification"}

    async def _make_plan(self, state: TravelPlanState) -> Dict[str, Any]:
        await self._emit("plan_started", {"revision": state.get("revision_count", 0)})
        plan = await _resolve(self.planner(
            state["original_request"],
            state["context"],
            previous_plan=None,
            feedback=None,
            on_token=self.on_token,
        ))
        return {
            "current_plan": plan,
            "plan_versions": [plan],
            "status": "planned",
        }

    async def _build_steps(self, state: TravelPlanState) -> Dict[str, Any]:
        steps = self._parse_steps(state["current_plan"])
        await self._emit("steps_built", {"count": len(steps)})
        return {"steps": steps}

    async def _execute_steps(self, state: TravelPlanState) -> Dict[str, Any]:
        executed = []
        steps = []
        for step in state.get("steps", []):
            item = dict(step)
            try:
                result = await _resolve(self.executor(item, state))
                if not isinstance(result, dict):
                    result = {"status": "completed", "result": str(result), "source": "executor"}
                item.update({
                    "status": result.get("status", "completed"),
                    "result": result.get("result", ""),
                    "error": result.get("error", ""),
                    "source": result.get("source", "executor"),
                })
            except Exception as exc:
                item.update({
                    "status": "failed",
                    "result": "",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
            steps.append(item)
            executed.append({
                "id": item["id"],
                "status": item["status"],
                "result": item["result"],
                "error": item["error"],
            })
        await self._emit("steps_executed", {"steps": executed})
        return {
            "steps": steps,
            "step_results": state.get("step_results", []) + executed,
            "status": "executed",
        }

    async def _reflect(self, state: TravelPlanState) -> Dict[str, Any]:
        reflection = self._normalize_reflection(await _resolve(self.reflector(
            state["original_request"],
            state["current_plan"],
        )))
        await self._emit("reflection", reflection)
        return {"reflection": reflection, "status": "reflected"}

    def _should_continue(self, state: TravelPlanState) -> str:
        reflection = state.get("reflection", {})
        if reflection.get("is_satisfactory", True):
            return "finalize"
        if state.get("revision_count", 0) >= self.max_revisions:
            return "finalize"
        return "revise"

    async def _revise(self, state: TravelPlanState) -> Dict[str, Any]:
        feedback = "\n".join(state.get("reflection", {}).get("suggestions", []))
        await self._emit("revision", {
            "revision_count": state.get("revision_count", 0) + 1,
            "feedback": feedback,
        })
        plan = await _resolve(self.planner(
            state["original_request"],
            state["context"],
            previous_plan=state["current_plan"],
            feedback=feedback,
            on_token=self.on_token,
        ))
        return {
            "current_plan": plan,
            "plan_versions": state.get("plan_versions", []) + [plan],
            "revision_count": state.get("revision_count", 0) + 1,
            "status": "revised",
        }

    async def _finalize(self, state: TravelPlanState) -> Dict[str, Any]:
        satisfactory = state.get("reflection", {}).get("is_satisfactory", True)
        status = "approved" if satisfactory else "max_revisions_reached"
        await self._emit("finalized", {"status": status, "revision_count": state.get("revision_count", 0)})
        return {"final_plan": state.get("current_plan", ""), "status": status}

    def _build_graph(self):
        graph = StateGraph(TravelPlanState)
        graph.add_node("extract_requirements", self._extract)
        graph.add_node("clarify", self._clarify)
        graph.add_node("make_plan", self._make_plan)
        graph.add_node("build_steps", self._build_steps)
        graph.add_node("execute_steps", self._execute_steps)
        graph.add_node("reflect", self._reflect)
        graph.add_node("revise", self._revise)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "extract_requirements")
        graph.add_conditional_edges(
            "extract_requirements",
            self._should_plan,
            {"clarify": "clarify", "make_plan": "make_plan"},
        )
        graph.add_edge("clarify", END)
        graph.add_edge("make_plan", "build_steps")
        graph.add_edge("build_steps", "execute_steps")
        graph.add_edge("execute_steps", "reflect")
        graph.add_conditional_edges(
            "reflect",
            self._should_continue,
            {"revise": "revise", "finalize": "finalize"},
        )
        graph.add_edge("revise", "build_steps")
        graph.add_edge("finalize", END)
        return graph.compile()

    async def run_streaming(
        self,
        original_request: str,
        user_id: str,
        context: str = "",
        thread_id: Optional[str] = None,
        chat_history: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        initial: TravelPlanState = {
            "original_request": original_request,
            "user_id": user_id,
            "chat_history": list(chat_history or []),
            "context": context,
            "requirements": {},
            "clarifications": [],
            "plan_versions": [],
            "current_plan": "",
            "steps": [],
            "step_results": [],
            "reflection": {},
            "revision_count": 0,
            "final_plan": "",
            "status": "started",
        }
        return await self.graph.ainvoke(
            initial,
            config={"configurable": {"thread_id": thread_id or f"travel:{user_id}"}},
        )

    def run(
        self,
        original_request: str,
        user_id: str,
        context: str = "",
        thread_id: Optional[str] = None,
        chat_history: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        return asyncio.run(self.run_streaming(
            original_request,
            user_id,
            context,
            thread_id,
            chat_history,
        ))
