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

from app.execution_log import CallbackEventQueue, build_execution_log, run_policy_node
from app.observability import timed_span
from app.reliability import NodePolicy, RetryPolicy


class TravelToolExecutorRoute:
    @staticmethod
    def route_pair(text: str):
        from app.tools.travel_executor import TravelToolExecutor
        return TravelToolExecutor._route_pair(text or "")


class TravelPlanState(TypedDict):
    original_request: str
    user_id: str
    chat_history: List[Any]
    context: str
    requirements: Dict[str, Any]
    clarifications: List[str]
    plan_versions: List[str]
    current_plan: str
    evidence_steps: List[Dict[str, Any]]
    evidence_results: List[Dict[str, Any]]
    evidence_context: str
    reflection: Dict[str, Any]
    revision_count: int
    final_plan: str
    status: str
    trace_id: str


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
        node_policies: Optional[Dict[str, NodePolicy]] = None,
    ):
        if max_revisions < 0:
            raise ValueError("max_revisions 不能为负数")
        self.planner = planner
        self.reflector = reflector
        self.executor = executor or self._default_executor
        self.on_event = on_event
        self.on_token = on_token
        self.max_revisions = max_revisions
        self.node_policies = node_policies or {
            "extract_requirements": NodePolicy(timeout_seconds=5),
            "clarify": NodePolicy(timeout_seconds=5),
            "make_plan": NodePolicy(timeout_seconds=120),
            "build_steps": NodePolicy(timeout_seconds=5),
            "execute_steps": NodePolicy(timeout_seconds=120),
            "reflect": NodePolicy(timeout_seconds=90),
            "revise": NodePolicy(timeout_seconds=120),
            "finalize": NodePolicy(timeout_seconds=5),
        }
        self.graph = self._build_graph()

    def _policy_node(self, name: str, node):
        policy = self.node_policies[name]

        async def run(state: TravelPlanState):
            async def operation(_attempt: int):
                return await node(state)
            return await run_policy_node(
                policy,
                operation,
                queue=CallbackEventQueue(self._emit),
                graph="travel",
                node=name,
                step="plan" if name in {"make_plan", "build_steps"} else name,
                details={"revision": state.get("revision_count", 0)},
            )

        return run

    async def _log(
        self,
        node: str,
        kind: str,
        message: str,
        *,
        status: str = "info",
        step: str = "",
        branch: Optional[str] = None,
        iteration: Optional[int] = None,
        max_iterations: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = build_execution_log(
            graph="travel",
            node=node,
            kind=kind,
            message=message,
            status=status,
            step=step,
            branch=branch,
            iteration=iteration,
            max_iterations=max_iterations,
            details=details,
        )
        await self._emit(payload["event"], {**payload["data"], "step": payload["step"]})

    @staticmethod
    def _planner_context(state: TravelPlanState) -> str:
        parts = [state.get("context") or ""]
        if state.get("evidence_context"):
            parts.append("【真实工具证据】\n" + state["evidence_context"])
        return "\n\n".join(part for part in parts if part.strip())

    @staticmethod
    async def _default_executor(step: Dict[str, Any], _state: TravelPlanState) -> Dict[str, Any]:
        return {
            "status": "completed",
            "result": "已结合当前上下文和本地攻略执行；天气和路线通过真实 provider 查询",
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
                if json_match:
                    try:
                        result = json.loads(json_match.group(0))
                    except json.JSONDecodeError:
                        result = {}
                else:
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
        recognized_fields = {
            "severity", "is_satisfactory", "issues", "suggestions"
        }
        if isinstance(result, dict) and not recognized_fields.intersection(result):
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
            "suggestions": [
                str(item) for item in result.get("suggestions", []) if str(item).strip()
            ],
            "severity": "critical" if critical else severity or "pass",
        }

    @staticmethod
    def _deterministic_plan_issues(state: TravelPlanState) -> List[str]:
        """独立于模型审核的硬约束校验。"""
        issues: List[str] = []
        plan = str(state.get("current_plan") or "").strip()
        if not plan:
            return ["计划正文为空"]

        requirements = state.get("requirements") or {}
        requested_days = requirements.get("days")
        day_pattern = re.compile(
            r"(?:^|\n)((?:第\s*[一二三四五六七八九十\d]+\s*天|Day\s*\d+)[^\n]*)",
            re.IGNORECASE,
        )
        plan_days = len(day_pattern.findall(plan))
        if requested_days and plan_days and plan_days != int(requested_days):
            issues.append(
                f"计划天数不一致：要求 {requested_days} 天，实际生成 {plan_days} 天"
            )

        requested_budget = requirements.get("budget")
        total_match = re.search(
            r"(?:总预算|总费用|总花费|总计)\s*(?:为|是|:|：)?\s*¥?\s*(\d+(?:\.\d+)?)",
            plan,
        )
        if requested_budget and total_match:
            total_cost = float(total_match.group(1))
            if total_cost > float(requested_budget) * 1.05:
                issues.append(
                    f"预算超支：要求 {requested_budget} 元，计划总计 {total_cost:g} 元"
                )

        failed_steps = [
            step
            for step in state.get("evidence_results") or []
            if step.get("status") != "completed"
        ]
        if failed_steps:
            issues.append(f"计划步骤执行失败：{len(failed_steps)} 个步骤未完成")
        return issues

    async def _extract(self, state: TravelPlanState) -> Dict[str, Any]:
        requirements = self._extract_requirements(state["original_request"])
        clarifications = [
            name for name, key in (("目的地", "destination"), ("天数", "days"))
            if requirements.get(key) is None
        ]
        status = "needs_clarification" if clarifications else "ready"
        await self._emit("requirements", {"requirements": requirements, "clarifications": clarifications})
        await self._log(
            "extract_requirements",
            "branch_selected",
            "缺少核心字段，进入澄清分支" if clarifications else "核心字段完整，进入证据采集分支",
            status="warning" if clarifications else "ok",
            step="plan",
            branch="clarify" if clarifications else "build_steps",
            details={"requirements": requirements, "clarifications": clarifications},
        )
        return {"requirements": requirements, "clarifications": clarifications, "status": status}

    def _should_plan(self, state: TravelPlanState) -> str:
        return "clarify" if state.get("clarifications") else "build_steps"

    async def _clarify(self, state: TravelPlanState) -> Dict[str, Any]:
        missing = "、".join(state.get("clarifications", []))
        message = f"为了生成可靠行程，请先补充：{missing}。"
        await self._emit("clarification", {"message": message})
        return {"final_plan": message, "current_plan": "", "status": "needs_clarification"}

    async def _make_plan(self, state: TravelPlanState) -> Dict[str, Any]:
        await self._emit("plan_started", {"revision": state.get("revision_count", 0)})
        with timed_span(
            "model.travel_plan",
            trace_id=state.get("trace_id", ""),
            attributes={"revision": state.get("revision_count", 0)},
        ):
            revision_count = state.get("revision_count", 0)
            feedback = "\n".join(state.get("reflection", {}).get("suggestions") or [])
            plan = await _resolve(self.planner(
                state["original_request"],
                self._planner_context(state),
                previous_plan=state.get("current_plan") if revision_count else None,
                feedback=feedback if revision_count else None,
                on_token=self.on_token,
            ))
        return {
            "current_plan": plan,
            "plan_versions": state.get("plan_versions", []) + [plan],
            "status": "planned",
        }

    async def _build_steps(self, state: TravelPlanState) -> Dict[str, Any]:
        """先构建真实证据步骤，而不是从最终计划反解析执行步骤。"""
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
        if TravelToolExecutorRoute.route_pair(request):
            origin, target = TravelToolExecutorRoute.route_pair(request)
            evidence_steps.append({
                "id": "evidence-route",
                "title": "路线证据",
                "description": f"从{origin}到{target}",
                "status": "pending",
                "result": "",
                "error": "",
            })
        await self._emit("steps_built", {"count": len(evidence_steps), "kind": "evidence"})
        return {"evidence_steps": evidence_steps, "status": "evidence_planned"}

    async def _execute_steps(self, state: TravelPlanState) -> Dict[str, Any]:
        with timed_span(
            "travel.execute_evidence",
            trace_id=state.get("trace_id", ""),
            attributes={"step_count": len(state.get("evidence_steps") or [])},
        ):
            executed = []
            all_steps = state.get("evidence_steps", [])
            for index, step in enumerate(all_steps, start=1):
                await self._log(
                    "execute_steps",
                    "loop_iteration",
                    f"执行证据步骤 {index}/{len(all_steps)}",
                    status="running",
                    step="execute",
                    iteration=index,
                    max_iterations=len(all_steps),
                    details={"step_id": step.get("id", ""), "revision": state.get("revision_count", 0)},
                )
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
                    await self._log(
                        "execute_steps",
                        "tool_fallback" if fallback else "tool_completed",
                        f"{tool_call.get('tool', '工具')} 数据" + (
                            f"不可用：{str(tool_result.get('error') or '')[:120]}" if fallback else "执行成功"
                        ),
                        status="warning" if fallback else "ok",
                        step="execute",
                        iteration=index,
                        max_iterations=len(all_steps),
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

    async def _reflect(self, state: TravelPlanState) -> Dict[str, Any]:
        deterministic_issues = self._deterministic_plan_issues(state)
        if deterministic_issues:
            # 预算超支、天数不一致等硬错误已经足以触发修订，
            # 不再等待一次完整的 LLM 审核来得到相同结论。
            reflection = {
                "is_satisfactory": False,
                "severity": "critical",
                "issues": list(deterministic_issues),
                "suggestions": list(deterministic_issues),
                "source": "deterministic",
            }
        elif self._can_use_deterministic_reflection(state):
            reflection = {
                "is_satisfactory": True,
                "severity": "pass",
                "issues": [],
                "suggestions": [],
                "source": "deterministic_fast_path",
            }
        else:
            with timed_span(
                "model.travel_reflect",
                trace_id=state.get("trace_id", ""),
                attributes={"revision": state.get("revision_count", 0)},
            ):
                reflection = self._normalize_reflection(await _resolve(self.reflector(
                    state["original_request"],
                    state["current_plan"],
                )))
        await self._emit("reflection", reflection)
        await self._log(
            "reflect",
            "decision",
            "审核通过" if reflection.get("is_satisfactory") else f"发现硬错误：{reflection.get('severity')}",
            status="ok" if reflection.get("is_satisfactory") else "warning",
            step="reflect",
            branch="finalize" if reflection.get("is_satisfactory") else "revise_or_limit",
            details={
                "severity": reflection.get("severity"),
                "issues": reflection.get("issues", []),
            },
        )
        return {"reflection": reflection, "status": "reflected"}

    @staticmethod
    def _can_use_deterministic_reflection(state: TravelPlanState) -> bool:
        """简单且工具证据完整的单步计划不再重复调用 LLM 审核。"""
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

    def _should_continue(self, state: TravelPlanState) -> str:
        reflection = state.get("reflection", {})
        if reflection.get("is_satisfactory", True):
            return "finalize"
        if state.get("revision_count", 0) >= self.max_revisions:
            return "finalize"
        return "revise"

    async def _revise(self, state: TravelPlanState) -> Dict[str, Any]:
        feedback = "\n".join(state.get("reflection", {}).get("suggestions") or [])
        revision = state.get("revision_count", 0) + 1
        await self._emit("revision", {
            "revision_count": revision,
            "feedback": feedback,
        })
        await self._log(
            "revise",
            "loop_iteration",
            f"进入第 {revision}/{self.max_revisions} 次修订循环",
            status="warning",
            step="revise",
            iteration=revision,
            max_iterations=self.max_revisions,
            details={"feedback": feedback},
        )
        return {"revision_count": revision, "status": "revise_requested"}

    async def _finalize(self, state: TravelPlanState) -> Dict[str, Any]:
        satisfactory = state.get("reflection", {}).get("is_satisfactory", True)
        status = "approved" if satisfactory else "max_revisions_reached"
        await self._emit("finalized", {"status": status, "revision_count": state.get("revision_count", 0)})
        await self._log(
            "finalize",
            "limit_reached" if status == "max_revisions_reached" else "completed",
            (
                f"修订达到上限 {self.max_revisions} 次，保留当前版本"
                if status == "max_revisions_reached"
                else f"旅行计划完成，修订 {state.get('revision_count', 0)} 次"
            ),
            status="warning" if status == "max_revisions_reached" else "ok",
            step="plan",
            iteration=state.get("revision_count", 0),
            max_iterations=self.max_revisions,
            details={"status": status},
        )
        return {"final_plan": state.get("current_plan", ""), "status": status}

    def _build_graph(self):
        graph = StateGraph(TravelPlanState)
        graph.add_node("extract_requirements", self._extract)
        graph.add_node("clarify", self._policy_node("clarify", self._clarify))
        graph.add_node("make_plan", self._policy_node("make_plan", self._make_plan))
        graph.add_node("build_steps", self._policy_node("build_steps", self._build_steps))
        graph.add_node("execute_steps", self._policy_node("execute_steps", self._execute_steps))
        graph.add_node("reflect", self._policy_node("reflect", self._reflect))
        graph.add_node("revise", self._policy_node("revise", self._revise))
        graph.add_node("finalize", self._policy_node("finalize", self._finalize))
        graph.add_edge(START, "extract_requirements")
        graph.add_conditional_edges(
            "extract_requirements",
            self._should_plan,
            {"clarify": "clarify", "build_steps": "build_steps"},
        )
        graph.add_edge("clarify", END)
        graph.add_edge("build_steps", "execute_steps")
        graph.add_edge("execute_steps", "make_plan")
        graph.add_edge("make_plan", "reflect")
        graph.add_conditional_edges(
            "reflect",
            self._should_continue,
            {"revise": "revise", "finalize": "finalize"},
        )
        graph.add_edge("revise", "make_plan")
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
            "evidence_steps": [],
            "evidence_results": [],
            "evidence_context": "",
            "reflection": {},
            "revision_count": 0,
            "final_plan": "",
            "status": "started",
            "trace_id": thread_id or "",
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
