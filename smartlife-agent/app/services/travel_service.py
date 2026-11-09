"""旅行领域服务。

只提供**能力**：prompt、规划/审核模型调用、工具执行、上下文构建、轻量对话。
控制流（需求抽取 → 证据 → 计划 → 审核 → 修订）在
``app.graphs.subgraphs.travel`` 的图里。
"""
import os
import re
from datetime import date
from typing import Dict, List, Any, Optional
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from app.config import create_llm, create_small_llm
from app.memory.long_term import LongTermMemory
from app.llm_json import ainvoke_json, invoke_json
from app.observability import extract_model_usage, timed_span
from app.streaming import EventType
from app.tools.travel_executor import TravelToolExecutor


class TravelReflection(BaseModel):
    severity: str = "pass"
    is_satisfactory: bool = True
    issues: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)


class TravelService:
    """旅行领域服务：prompt、规划/审核调用、工具执行、上下文构建。"""

    def __init__(self, model_name: str = None):
        self.model_name = model_name
        self.llm = create_llm(model_name)
        self.reflect_llm = create_small_llm(max_tokens=1200)
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
3. 过期或来源不明的数据不得写成“当前”“实时”或确定价格；
4. 活动证据里只有**场所**（场馆、景点、公园），没有场次、档期和票价。不得声称某场馆“正在上演”某演出，只能说“可到官方渠道查档期”；用户问具体演出时如实说明拿不到，不要把常识当成当期排期。

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
        """获取真实天气证据；没有真实数据时返回禁止猜测的明确状态。

        行程有几天就查几天：只查出发那天，等于把第 2、3 天的天气留成空白，
        而大纲里又不会说"这几天没查过"。
        """
        from app.graphs.subgraphs.travel import extract_requirements

        requirements = extract_requirements(user_input)
        destination = str(requirements.get("destination") or "").strip()
        if not destination:
            return ""
        raw_date = str(requirements.get("start_date") or "").strip()
        travel_date = date.fromisoformat(raw_date) if raw_date else date.today()
        try:
            span = max(1, min(16, int(requirements.get("days") or 1)))
        except (TypeError, ValueError):
            span = 1

        if span > 1:
            forecast = await self.travel_tool_executor.get_weather_forecast(
                destination, travel_date, span
            )
            rows = forecast.get("days") if forecast.get("accepted") else None
            if rows:
                fetched_at = forecast.get("fetched_at")
                source = forecast.get("source", "外部天气 provider")
                lines = ["【真实工具证据】"]
                for index, row in enumerate(rows, start=1):
                    rain = row.get("precipitation_probability")
                    rain_text = f"，降水 {rain:g}%" if rain is not None else ""
                    lines.append(
                        f"- 第{index}天 {row.get('date', '')} {destination}天气："
                        f"{row.get('temperature', '')} {row.get('condition', '')}{rain_text}"
                    )
                lines.append(f"- 来源：{source}")
                if fetched_at:
                    lines[-1] += f"，获取时间戳：{fetched_at}"
                return "\n".join(lines)

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

    # 攻略文件是"主题"命名的，这里维护主题 -> 触发词，而不是全文关键词扫描。
    _GUIDE_TOPICS = (
        ("hangzhou", ("杭州", "西湖", "千岛湖", "灵隐")),
        ("camping", ("露营", "营地", "帐篷", "徒步", "登山", "户外")),
        ("couples", ("情侣", "二人世界", "夫妻", "蜜月", "纪念日")),
        ("budget", ("省钱", "预算", "便宜", "性价比", "穷游")),
    )

    def _read_local_guides(self, query: str) -> str:
        """按主题挑攻略文件；一个都对不上时退回前 3 篇做泛化参考。"""
        import glob

        base_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "guides"
        )
        if not os.path.exists(base_dir):
            return ""

        files = sorted(glob.glob(os.path.join(base_dir, "*.txt")))
        if not files:
            return ""

        text = str(query or "")
        selected = []
        for path in files:
            name = os.path.basename(path).lower()
            for key, terms in self._GUIDE_TOPICS:
                if key in name and any(term in text for term in terms):
                    selected.append(path)
                    break
        if not selected:
            selected = files[:3]

        relevant = []
        for path in selected:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    relevant.append(f"【{os.path.basename(path)}】\n{handle.read()[:1500]}")
            except Exception:
                continue
        return "\n\n".join(relevant)

    def _build_plan_context(
        self,
        user_input: str,
        user_id: str,
        chat_history: Optional[List[Any]] = None,
        constraints: str = "",
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
        if constraints:
            parts.append(f"【共享约束】\n{constraints}")
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
        reflect_llm = llm or getattr(self, "reflect_llm", self.llm)
        with timed_span(
            "model.travel_reflect_usage",
            trace_id=trace_id,
        ) as measurement:
            result = await ainvoke_json(
                reflect_llm,
                self.reflect_prompt,
                TravelReflection,
                {"original_request": request, "plan": plan},
            )
        return result

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
        return invoke_json(
            getattr(self, "reflect_llm", self.llm),
            self.reflect_prompt,
            TravelReflection,
            {"original_request": request, "plan": plan},
        )

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
            queue: 事件出口（EventSink，由调用方节点提供）

        Returns:
            str: 回复文本
        """
        guide_context = self._read_local_guides(user_input)
        memory_context = self._get_memory_context(user_id, query=user_input)
        parts = [user_input]
        if guide_context:
            parts.append("【参考攻略】\n" + guide_context)
        if memory_context:
            parts.append(memory_context)
        full_input = "\n\n".join(parts)

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
