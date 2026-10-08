"""
Travel Agent - Plan & Execute 行程规划
支持同步 + 异步流式两种调用方式
"""
import asyncio
import os
import re
from datetime import date
from functools import partial
from typing import Dict, List, Any, Optional, AsyncGenerator
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from app.config import create_llm
from app.memory.long_term import LongTermMemory
from app.agents.travel_graph import TravelPlanGraph
from app.observability import extract_model_usage, timed_span
from app.streaming import EventType
from app.tools.travel_executor import TravelToolExecutor


class TravelPlan(BaseModel):
    """旅行计划"""
    destination: str = Field(description="目的地")
    days: int = Field(description="天数")
    budget: float = Field(description="预算")
    itinerary: List[Dict[str, Any]] = Field(description="每日行程")
    total_cost: float = Field(description="总费用")
    tips: List[str] = Field(description="旅行建议")


class TravelAgent:
    """旅游 Agent - Plan & Execute 模式"""

    def __init__(self, model_name: str = None):
        self.model_name = model_name
        self.llm = create_llm(model_name)
        self._memory = None
        self.travel_tool_executor = TravelToolExecutor()

        self.plan_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个旅行规划专家。根据用户需求制定详细的旅行计划。

能力：
1. 查询目的地天气
2. 推荐景点和活动
3. 规划行程路线
4. 计算预算

规划原则：
1. 每天不超过3-4个景点，不要太赶
2. 合理安排地理位置，避免来回跑
3. 预留用餐和休息时间
4. 考虑天气因素
5. 控制在预算范围内

实时事实规则：
1. 只有上下文中标记为【真实工具证据】的天气、路线数据可以当作事实引用；
2. 没有真实天气证据时，明确说明“未获取到可验证的实时天气”，不得使用模拟值、季节典型值或猜测温度冒充当前天气；
3. 过期或来源不明的数据不得写成“当前”“实时”或确定价格。

用户 Memory 规则：
- 上下文中的用户偏好来自已保存的长期 Memory，可用于个性化；
- 与本轮需求冲突时，以用户本轮明确要求为准。

用中文回复，输出清晰、完整的最终行程正文。审核宽松，仅在严重硬错误时按需修订。不要输出版本说明、审核附注、改进说明或重复标题。"""),
            ("user", "{input}")
        ])

        self.reflect_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是旅行计划审核专家。审核要宽松，只标记会实际影响执行的严重硬错误。

严重硬错误仅包括：
1. 天数明确不符合用户要求
2. 总预算明确超支，或分项明显算错
3. 每日路线明确冲突、无法执行
4. 明确遗漏目的地、天数、预算等核心约束
5. 把猜测或未经验证的天气/路线数据写成当前真实信息

风格、措辞、可选优化、轻微偏好差异和一般改进建议都不算严重问题。
不确定时优先判 pass；不要为了“更完美”而要求重写。
严格返回 JSON，不要其他文字：
{{"severity": "pass", "is_satisfactory": true, "issues": [], "suggestions": []}}
severity 只能是 pass、minor、critical。只有 critical 才设置 is_satisfactory=false；minor 只能放进 suggestions。"""),
            ("user", "原始请求：{original_request}\n\n生成的计划：{plan}")
        ])

        self.revise_prompt = ChatPromptTemplate.from_messages([
            ("system", "你是旅行规划专家。请只修复审核指出的预算、天数或路线硬错误，并输出一份完整、唯一的最终行程。不要输出审核意见、修订说明、版本附注或重复标题。"),
            ("user", "原始请求：{original_request}\n\n上一版计划：{plan}\n\n审核意见：{feedback}\n\n请输出完整改进后的计划。")
        ])

        self.chat_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是旅游规划专家。根据用户需求提供目的地推荐、景点介绍、出行建议。
用中文回复，内容实用、结构清晰。"""),
            ("user", "{input}")
        ])

    @property
    def memory(self):
        if self._memory is None:
            try:
                self._memory = LongTermMemory()
            except Exception:
                pass
        return self._memory

    def _get_memory_context(self, user_id: str, query: str = "") -> str:
        """获取跨场景记忆上下文"""
        if not self.memory:
            return ""
        cross_memories = self.memory.get_cross_scene_memories(user_id, "travel", query=query)
        if cross_memories:
            return "\n用户长期 Memory（跨场景偏好）：\n" + "\n".join(
                [m["content"] for m in cross_memories[:3]]
            )
        return ""

    async def get_weather_context(self, user_input: str) -> str:
        """获取真实天气证据；没有真实数据时返回禁止猜测的明确状态。"""
        from app.agents.travel_graph import TravelPlanGraph

        requirements = TravelPlanGraph._extract_requirements(user_input)
        destination = str(requirements.get("destination") or "").strip()
        if not destination:
            return ""
        raw_date = str(requirements.get("start_date") or "").strip()
        travel_date = date.fromisoformat(raw_date) if raw_date else date.today()
        weather = await self.travel_tool_executor.get_weather_evidence(
            destination,
            travel_date,
        )
        if weather.get("accepted"):
            fetched_at = weather.get("fetched_at")
            source = weather.get("source", "外部天气 provider")
            return (
                "【真实工具证据】\n"
                f"- {travel_date.isoformat()} {destination}天气："
                f"{weather.get('temperature', '')} {weather.get('condition', '')}，"
                f"湿度 {weather.get('humidity', '')}\n"
                f"- 来源：{source}"
                + (f"，获取时间戳：{fetched_at}" if fetched_at else "")
            )
        return (
            "【天气工具状态】未获取到可验证的真实天气数据；"
            "模拟结果已拒绝。请明确披露天气不可用，禁止给出猜测温度或把季节典型值写成当前天气。"
        )

    def _read_local_guides(self, query: str) -> str:
        """读取本地攻略文件"""
        import glob
        base_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "guides"
        )
        if not os.path.exists(base_dir):
            return ""
        guide_files = glob.glob(os.path.join(base_dir, "*.txt"))
        relevant = []
        query_lower = query.lower()
        keywords = ["杭州", "露营", "camping", "情侣", "budget", "预算", "西湖", "户外", "登山"]
        for fp in guide_files:
            fn = os.path.basename(fp).lower()
            if any(kw in fn or kw in query_lower for kw in keywords):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        relevant.append(f"【{os.path.basename(fp)}】\n{f.read()[:1500]}")
                except Exception:
                    pass
        if not relevant:
            for fp in guide_files[:3]:
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        relevant.append(f"【{os.path.basename(fp)}】\n{f.read(500)}")
                except Exception:
                    pass
        return "\n\n".join(relevant)

    def _build_plan_context(
        self,
        user_input: str,
        user_id: str,
        chat_history: Optional[List[Any]] = None,
    ) -> str:
        guide_context = self._read_local_guides(user_input)
        memory_context = self._get_memory_context(user_id, query=user_input)
        parts = []
        if chat_history:
            history_lines = []
            for message in list(chat_history)[-10:]:
                role = getattr(message, "type", getattr(message, "role", "message"))
                content = getattr(message, "content", str(message))
                history_lines.append(f"{role}: {content}")
            parts.append("【本轮对话历史】\n" + "\n".join(history_lines))
        if guide_context:
            parts.append(f"【参考攻略】\n{guide_context}")
        if memory_context:
            parts.append(memory_context)
        return "\n\n".join(parts)

    async def _planner_streaming(
        self,
        request: str,
        context: str,
        previous_plan: Optional[str] = None,
        feedback: Optional[str] = None,
        on_token=None,
        llm=None,
        trace_id: str = "",
    ) -> str:
        active_llm = llm or self.llm
        if previous_plan is None:
            chain = self.plan_prompt | active_llm
            values = {"input": f"{request}\n\n{context}".strip()}
            stage = "plan"
        else:
            chain = self.revise_prompt | active_llm
            values = {
                "original_request": request,
                "plan": previous_plan,
                "feedback": feedback or "请完善计划",
            }
            stage = "revise"

        result = ""
        with timed_span(
            "model.travel_generate_usage",
            trace_id=trace_id,
            attributes={"stage": stage},
        ) as measurement:
            async for chunk in chain.astream(values):
                measurement.add_attributes(extract_model_usage(chunk))
                if chunk.content:
                    result += chunk.content
                    if on_token:
                        await on_token(chunk.content, stage)
        return result

    async def _reflector_streaming(
        self,
        request: str,
        plan: str,
        llm=None,
        trace_id: str = "",
    ):
        chain = self.reflect_prompt | (llm or self.llm)
        with timed_span(
            "model.travel_reflect_usage",
            trace_id=trace_id,
        ) as measurement:
            result = await chain.ainvoke({"original_request": request, "plan": plan})
            measurement.add_attributes(extract_model_usage(result))
        return result.content

    async def _planner_sync_adapter(
        self,
        request: str,
        context: str,
        previous_plan: Optional[str] = None,
        feedback: Optional[str] = None,
        on_token=None,
    ) -> str:
        if previous_plan is None:
            messages = self.plan_prompt.format_messages(input=f"{request}\n\n{context}".strip())
        else:
            messages = self.revise_prompt.format_messages(
                original_request=request,
                plan=previous_plan,
                feedback=feedback or "请完善计划",
            )
        result = self.llm.invoke(messages)
        return result.content

    async def _reflector_sync_adapter(self, request: str, plan: str):
        messages = self.reflect_prompt.format_messages(original_request=request, plan=plan)
        return self.llm.invoke(messages).content

    async def _execute_plan_step(self, step: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        """执行真实天气和路线工具。"""
        return await self.travel_tool_executor.execute(step, state)

    @staticmethod
    def _remove_revision_notes(plan: str) -> str:
        """不让审核/修订附注进入最终用户正文。"""
        return re.sub(
            r"\n+(?:改进说明|审核意见附注|修订说明|版本说明)\s*[:：].*$",
            "",
            plan or "",
            flags=re.DOTALL,
        ).strip()

    # ========== 同步接口（兼容旧代码）==========

    def plan_trip(
        self,
        user_input: str,
        user_id: str = "user_001",
        chat_history: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """同步规划旅行 - 与流式入口共享 TravelPlanGraph。"""
        context_parts = [self._build_plan_context(user_input, user_id, chat_history)]
        try:
            weather_context = asyncio.run(self.get_weather_context(user_input))
        except RuntimeError:
            weather_context = ""
        if weather_context:
            context_parts.append(weather_context)
        context = "\n\n".join(part for part in context_parts if part.strip())
        graph = TravelPlanGraph(
            planner=self._planner_sync_adapter,
            reflector=self._reflector_sync_adapter,
            executor=self._execute_plan_step,
            max_revisions=3,
        )
        state = graph.run(
            user_input,
            user_id,
            context,
            thread_id=f"travel:{user_id}:sync",
            chat_history=chat_history,
        )
        return {
            "plan": self._remove_revision_notes(state["final_plan"]),
            "agent": "travel",
            "memories_used": 1 if context else 0,
            "iterations": state["revision_count"] + 1,
            "plan_versions": state["plan_versions"],
            "steps": state["steps"],
            "reflection": state["reflection"],
            "status": state["status"],
        }

    def chat(self, user_input: str, user_id: str = "user_001") -> Dict[str, Any]:
        """同步旅游问答（非规划类）"""
        memory_context = self._get_memory_context(user_id, query=user_input)
        full_input = user_input + memory_context

        messages = self.chat_prompt.format_messages(input=full_input)
        response = self.llm.invoke(messages)

        return {
            "response": response.content,
            "agent": "travel",
            "memories_used": 1 if memory_context else 0
        }

    # ========== 异步流式接口 ==========

    async def plan_trip_streaming(
        self,
        user_input: str,
        user_id: str = "user_001",
        queue=None,
        chat_history: Optional[List[Any]] = None,
    ) -> str:
        """
        异步流式规划旅行

        Args:
            user_input: 用户输入
            user_id: 用户ID
            queue: EventQueue 事件队列

        Returns:
            str: 最终计划文本
        """
        if queue:
            await queue.emit(EventType.STEP, {
                "step": 2,
                "total": 4,
                "name": "资料检索与准备",
                "description": "正在读取本地攻略、当前需求和长期跨场景 MD 记忆",
                "status": "active",
            }, step="retrieval")
        context_parts = [self._build_plan_context(user_input, user_id, chat_history)]
        weather_context = await self.get_weather_context(user_input)
        if weather_context:
            context_parts.append(weather_context)
        context = "\n\n".join(part for part in context_parts if part.strip())
        request_llm = create_llm(getattr(self, "model_name", None))

        async def on_token(token: str, stage: str):
            if queue:
                await queue.emit_token(token, step=stage)

        emitted_steps = {2} if queue else set()

        async def emit_step3(description: str, status: str):
            # 一个步骤只广播一次 STEP，内部阶段变化由 token/reset 事件表达，
            # 避免时间线重复插入同一个步骤。
            if queue and 3 not in emitted_steps:
                emitted_steps.add(3)
                await queue.emit(EventType.STEP, {
                    "step": 3,
                    "total": 4,
                    "name": "生成并审核回复",
                    "description": description,
                    "status": status,
                }, step="generate")

        async def on_event(event: str, data: Dict[str, Any]):
            if not queue:
                return
            if event == "execution_log":
                await queue.emit_execution_log({
                    "event": event,
                    "data": data,
                    "step": data.get("step", ""),
                })
            elif event == "plan_started":
                revision = int(data.get("revision", 0) or 0)
                if revision:
                    await emit_step3(f"发现严重硬错误，正在修订（第 {revision + 1}/3 次）", "active")
                else:
                    await emit_step3("正在生成完整行程，并准备宽松硬错误检查", "active")
            elif event == "steps_built":
                await emit_step3("只核对预算总额、天数、路线和明显无法执行的硬错误", "active")
            elif event == "steps_executed":
                await emit_step3("严重硬错误检查完成", "active")
            elif event == "reflection":
                if data.get("is_satisfactory"):
                    await emit_step3("宽松审核通过，无严重硬错误", "completed")
                else:
                    await emit_step3("发现严重硬错误，准备按需修订", "active")
            elif event == "revision":
                await queue.emit("response_reset", {}, step="revise")
                count = int(data.get("revision_count", 1) or 1)
                await emit_step3(f"正在修订严重硬错误（第 {count}/3 次）", "active")
            elif event == "finalized":
                await emit_step3("最终行程已生成并通过宽松审核", "completed")
            elif event == "clarification":
                await queue.emit_token(data.get("message", ""), step="clarify")

        graph = TravelPlanGraph(
            planner=partial(
                self._planner_streaming,
                llm=request_llm,
                trace_id=f"travel:{user_id}:stream",
            ),
            reflector=partial(
                self._reflector_streaming,
                llm=request_llm,
                trace_id=f"travel:{user_id}:stream",
            ),
            executor=self._execute_plan_step,
            on_event=on_event,
            on_token=on_token,
            max_revisions=3,
        )
        state = await graph.run_streaming(
            user_input,
            user_id,
            context,
            thread_id=f"travel:{user_id}:stream",
            chat_history=chat_history,
        )
        return self._remove_revision_notes(state["final_plan"])

    async def chat_streaming(
        self,
        user_input: str,
        user_id: str = "user_001",
        queue=None
    ) -> str:
        """
        异步流式旅游问答

        Args:
            user_input: 用户输入
            user_id: 用户ID
            queue: EventQueue 事件队列

        Returns:
            str: 回复文本
        """
        memory_context = self._get_memory_context(user_id, query=user_input)
        full_input = user_input + memory_context

        if queue:
            await queue.emit(EventType.STEP, {
                "step": 2,
                "total": 4,
                "name": "旅游资料检索",
                "description": "读取本地攻略和跨场景偏好，为旅游问答准备上下文",
            }, step="retrieval")
            await queue.emit(EventType.STEP, {
                "step": 3,
                "total": 4,
                "name": "生成回复",
                "description": "大模型流式生成旅游建议",
            }, step="generate")
            await queue.emit_thinking("🧠 正在为您查询旅游信息...", step="generate")

        chain = self.chat_prompt | self.llm
        full_response = ""

        with timed_span(
            "model.travel_chat",
            trace_id=getattr(queue, "trace_id", ""),
            attributes={"user_id": user_id},
        ) as measurement:
            async for chunk in chain.astream({"input": full_input}):
                measurement.add_attributes(extract_model_usage(chunk))
                if chunk.content:
                    full_response += chunk.content
                    if queue:
                        await queue.emit_token(chunk.content, step="generate")

        return full_response
