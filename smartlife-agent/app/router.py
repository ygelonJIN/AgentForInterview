"""
任务路由器 - 兼容层
底层已改为 UnifiedClassifier，此文件保留旧接口兼容性
"""
from typing import Literal
from pydantic import BaseModel, Field
from app.classifier import UnifiedClassifier, ClassificationResult, get_classifier


class TaskRoute(BaseModel):
    """任务路由结果（兼容旧接口）"""
    route: Literal["react", "plan_and_execute"] = Field(
        description="路由类型：react用于简单任务，plan_and_execute用于复杂多步任务"
    )
    reason: str = Field(
        description="路由理由"
    )
    complexity: Literal["simple", "complex"] = Field(
        description="任务复杂度"
    )
    estimated_steps: int = Field(
        description="预估步骤数",
        ge=1,
        le=10
    )


class TaskRouter:
    """
    任务路由器（兼容旧接口）
    底层使用 UnifiedClassifier，避免重复 LLM 调用
    """

    def __init__(self, model_name: str = None):
        self._classifier = get_classifier()

    def route(self, user_message: str) -> TaskRoute:
        """
        路由用户任务（同步）

        Args:
            user_message: 用户输入

        Returns:
            TaskRoute: 路由结果
        """
        result = self._classifier.classify(user_message)

        # 映射到旧接口
        complexity = "complex" if result.route == "plan_and_execute" else "simple"
        estimated_steps = 3 if result.route == "plan_and_execute" else 1

        return TaskRoute(
            route=result.route,
            reason=result.reason or f"意图: {result.intent}, 子意图: {result.sub_intent}",
            complexity=complexity,
            estimated_steps=estimated_steps
        )

    def should_use_plan_and_execute(self, user_message: str) -> bool:
        result = self.route(user_message)
        return result.route == "plan_and_execute"


def route_task(user_message: str) -> TaskRoute:
    """快速路由函数"""
    router = TaskRouter()
    return router.route(user_message)
