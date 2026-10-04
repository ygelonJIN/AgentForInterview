"""
Travel Agent - Plan & Execute 行程规划
支持同步 + 异步流式两种调用方式
"""
import json
import os
from typing import Dict, List, Any, Optional, AsyncGenerator
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from app.config import create_llm
from app.memory.long_term import LongTermMemory


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
        self.llm = create_llm(model_name)
        self._memory = None

        self.plan_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个旅行规划专家。根据用户需求制定详细的旅行计划。

能力：
1. 查询目的地天气
2. 推荐景点和活动
3. 搜索酒店信息
4. 规划行程路线
5. 计算预算

规划原则：
1. 每天不超过3-4个景点，不要太赶
2. 合理安排地理位置，避免来回跑
3. 预留用餐和休息时间
4. 考虑天气因素
5. 控制在预算范围内

用中文回复，输出清晰的行程安排。"""),
            ("user", "{input}")
        ])

        self.reflect_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是旅行计划审核专家。检查以下计划是否合理。

检查要点：
1. 时间安排是否合理（不要一天排8个景点）
2. 预算是否超支
3. 地理位置是否方便
4. 是否有遗漏（用餐、休息时间）
5. 天气是否适合

如果计划合理，直接说"计划合理，无需修改"。
如果有问题，指出具体问题和改进建议。
用中文回复。"""),
            ("user", "原始请求：{original_request}\n\n生成的计划：{plan}")
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

    def _get_memory_context(self, user_id: str) -> str:
        """获取跨场景记忆上下文"""
        if not self.memory:
            return ""
        cross_memories = self.memory.get_cross_scene_memories(user_id, "travel")
        if cross_memories:
            return "\n用户历史偏好（来自购物记忆）：\n" + "\n".join(
                [m["content"] for m in cross_memories[:3]]
            )
        return ""

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

    # ========== 同步接口（兼容旧代码）==========

    def plan_trip(self, user_input: str, user_id: str = "user_001") -> Dict[str, Any]:
        """同步规划旅行 - Plan & Execute + Reflection"""
        memory_context = self._get_memory_context(user_id)
        full_input = user_input + memory_context

        # Step 1: 生成初始计划
        plan_messages = self.plan_prompt.format_messages(input=full_input)
        plan_response = self.llm.invoke(plan_messages)
        current_plan = plan_response.content

        # Step 2: Reflection 自检（最多3次）
        iterations = 1
        for i in range(3):
            reflect_messages = self.reflect_prompt.format_messages(
                original_request=user_input,
                plan=current_plan
            )
            reflect_response = self.llm.invoke(reflect_messages)

            try:
                reflection = json.loads(reflect_response.content)
                if reflection.get("is_satisfactory", True):
                    break
                if reflection.get("suggestions"):
                    revise_messages = self.plan_prompt.format_messages(
                        input=f"{full_input}\n\n请根据以下建议改进计划：\n" + "\n".join(reflection["suggestions"])
                    )
                    revised = self.llm.invoke(revise_messages)
                    current_plan = revised.content
                    iterations = i + 2
            except (json.JSONDecodeError, KeyError):
                break

        return {
            "plan": current_plan,
            "agent": "travel",
            "memories_used": 1 if memory_context else 0,
            "iterations": iterations
        }

    def chat(self, user_input: str, user_id: str = "user_001") -> Dict[str, Any]:
        """同步旅游问答（非规划类）"""
        memory_context = self._get_memory_context(user_id)
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
        queue=None
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
        memory_context = self._get_memory_context(user_id)
        guide_context = self._read_local_guides(user_input)
        full_input = user_input
        if guide_context:
            full_input += f"\n\n【参考攻略】\n{guide_context}"
        if memory_context:
            full_input += memory_context

        # Step 1: 流式生成初始计划
        if queue:
            if guide_context:
                await queue.emit_tool_call("攻略检索", user_input[:60], step="retrieval")
                await queue.emit_tool_result("攻略检索", "已加载本地攻略", step="retrieval")
            await queue.emit_thinking("🧠 正在制定旅行计划...", step="plan")

        plan_chain = self.plan_prompt | self.llm
        current_plan = ""

        async for chunk in plan_chain.astream({"input": full_input}):
            if chunk.content:
                current_plan += chunk.content
                if queue:
                    await queue.emit_token(chunk.content, step="plan")

        # Step 2: 流式 Reflection 自检
        if queue:
            await queue.emit_thinking("🧠 正在审核计划合理性...", step="reflect")

        reflect_chain = self.reflect_prompt | self.llm
        reflect_result = await reflect_chain.ainvoke({
            "original_request": user_input,
            "plan": current_plan
        })

        # 判断是否需要修改
        needs_revision = False
        feedback = ""
        try:
            reflection = json.loads(reflect_result.content)
            if not reflection.get("is_satisfactory", True) and reflection.get("suggestions"):
                needs_revision = True
                feedback = "\n".join(reflection["suggestions"])
        except (json.JSONDecodeError, KeyError):
            # 非JSON格式，检查关键词
            content = reflect_result.content
            if "问题" in content or "建议" in content or "改进" in content:
                needs_revision = True
                feedback = content

        if needs_revision:
            if queue:
                await queue.emit_thinking("🧠 根据审核意见优化计划...", step="revise")

            revise_prompt = ChatPromptTemplate.from_messages([
                ("system", "你是旅行规划专家。根据审核意见改进旅行计划。用中文回复，输出完整改进后的计划。"),
                ("user", "原始请求：{original}\n\n审核意见：{feedback}\n\n请改进计划。")
            ])
            revise_chain = revise_prompt | self.llm
            revised_plan = ""

            async for chunk in revise_chain.astream({
                "original": user_input,
                "feedback": feedback
            }):
                if chunk.content:
                    revised_plan += chunk.content
                    if queue:
                        await queue.emit_token(chunk.content, step="revise")

            return revised_plan

        return current_plan

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
        memory_context = self._get_memory_context(user_id)
        full_input = user_input + memory_context

        if queue:
            await queue.emit_thinking("🧠 正在为您查询旅游信息...", step="generate")

        chain = self.chat_prompt | self.llm
        full_response = ""

        async for chunk in chain.astream({"input": full_input}):
            if chunk.content:
                full_response += chunk.content
                if queue:
                    await queue.emit_token(chunk.content, step="generate")

        return full_response
