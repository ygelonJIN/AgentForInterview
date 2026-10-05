"""
[COMPATIBILITY] 旧 ReflectionAgent。

当前旅游规划反思闭环位于 `app/agents/travel_graph.py`。
"""
from typing import Dict, List, Any, Optional
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_llm
from pydantic import BaseModel, Field

class ReflectionResult(BaseModel):
    """反思结果"""
    is_satisfactory: bool = Field(description="结果是否满意")
    issues: List[str] = Field(default_factory=list, description="发现的问题")
    suggestions: List[str] = Field(default_factory=list, description="改进建议")
    revised_plan: Optional[str] = Field(default=None, description="修订后的计划")

class ReflectionAgent:
    """Reflection Agent - 自检和优化"""
    
    def __init__(self, model_name: str = None):
        self.llm = create_llm(model_name, max_tokens=2048)
        
        self.plan_review_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个计划审核专家。检查以下计划是否合理。

检查要点：
1. 时间安排是否合理（不要一天排8个景点）
2. 预算是否超支
3. 地理位置是否方便（不要在城市两端来回跑）
4. 是否有遗漏（比如没安排吃饭时间）
5. 天气是否适合户外活动

原始请求：{original_request}
生成的计划：{plan}

请以JSON格式返回审核结果。"""),
            ("user", "请审核这个计划，指出问题并给出改进建议。")
        ])
        
        self.product_review_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个商品推荐审核专家。检查推荐结果是否合理。

检查要点：
1. 推荐商品是否匹配用户需求
2. 价格是否在用户预算范围内
3. 推荐理由是否有依据
4. 是否有明显更好的替代品
5. 是否覆盖了用户的所有需求

用户请求：{original_request}
推荐结果：{recommendation}

请以JSON格式返回审核结果。"""),
            ("user", "请审核这个推荐结果。")
        ])
    
    def reflect_plan(self, original_request: str, plan: str, max_iterations: int = 3) -> Dict[str, Any]:
        """反思-改进循环（用于行程/清单）"""
        current_plan = plan
        iterations = 0
        history = []
        
        for i in range(max_iterations):
            iterations += 1
            
            chain = self.plan_review_prompt | self.llm.with_structured_output(ReflectionResult)
            result = chain.invoke({
                "original_request": original_request,
                "plan": current_plan
            })
            
            history.append({
                "iteration": i + 1,
                "is_satisfactory": result.is_satisfactory,
                "issues": result.issues,
                "suggestions": result.suggestions
            })
            
            if result.is_satisfactory:
                return {
                    "final_plan": current_plan,
                    "iterations": iterations,
                    "history": history,
                    "status": "approved"
                }
            
            # 用建议改进
            if result.suggestions:
                improve_prompt = ChatPromptTemplate.from_messages([
                    ("system", "根据以下建议改进计划。原始请求：{original_request}"),
                    ("user", "当前计划：\n{plan}\n\n改进建议：\n{suggestions}\n\n请输出改进后的完整计划。")
                ])
                chain = improve_prompt | self.llm
                improved = chain.invoke({
                    "original_request": original_request,
                    "plan": current_plan,
                    "suggestions": "\n".join(result.suggestions)
                })
                current_plan = improved.content
        
        return {
            "final_plan": current_plan,
            "iterations": iterations,
            "history": history,
            "status": "max_iterations_reached"
        }
    
    def reflect_recommendation(self, original_request: str, recommendation: str) -> Dict[str, Any]:
        """反思推荐结果"""
        chain = self.product_review_prompt | self.llm.with_structured_output(ReflectionResult)
        result = chain.invoke({
            "original_request": original_request,
            "recommendation": recommendation
        })
        
        return {
            "is_satisfactory": result.is_satisfactory,
            "issues": result.issues,
            "suggestions": result.suggestions,
            "status": "approved" if result.is_satisfactory else "needs_improvement"
        }
