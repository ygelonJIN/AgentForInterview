"""
偏好分析模块
"""
from typing import Dict, List, Any
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_small_llm
from pydantic import BaseModel, Field

class UserPreference(BaseModel):
    """用户偏好"""
    user_id: str
    shopping_preferences: Dict[str, Any] = Field(default_factory=dict)
    travel_preferences: Dict[str, Any] = Field(default_factory=dict)
    budget_constraint: float = 0
    priority_tags: List[str] = Field(default_factory=list)

class PreferenceAnalyzer:
    """偏好分析器"""
    
    def __init__(self, model_name: str = None):
        self.llm = create_small_llm()
        
        self.analyze_prompt = ChatPromptTemplate.from_messages([
            ("system", """分析以下用户信息，提取偏好标签。

用户信息：{user_info}
历史购物：{purchase_history}
历史旅行：{travel_history}

输出JSON格式偏好，包含以下字段：
- shopping: preferred_categories, price_sensitivity, brand_preference
- travel: preferred_styles, activity_types, accommodation_preference
- budget: daily_limit, total_limit
- tags: 偏好标签列表如 户外爱好者, 美食达人, 性价比党 等"""),
            ("user", "请分析用户偏好，输出JSON格式。")
        ])
    
    def analyze(self, user_id: str, user_info: Dict, purchase_history: List = None, travel_history: List = None) -> UserPreference:
        """分析单个用户偏好"""
        try:
            chain = self.analyze_prompt | self.llm
            result = chain.invoke({
                "user_info": str(user_info),
                "purchase_history": str(purchase_history or []),
                "travel_history": str(travel_history or [])
            })
            import json
            prefs = json.loads(result.content)
        except Exception:
            prefs = {}
        
        return UserPreference(
            user_id=user_id,
            shopping_preferences=prefs.get("shopping", {}),
            travel_preferences=prefs.get("travel", {}),
            budget_constraint=prefs.get("budget", {}).get("total_limit", 0),
            priority_tags=prefs.get("tags", [])
        )
    
    def find_common_ground(self, preferences: List[UserPreference]) -> Dict[str, Any]:
        """找到多个用户的共同偏好"""
        all_tags: List[str] = []
        for pref in preferences:
            all_tags.extend(pref.priority_tags)
        
        tag_count: Dict[str, int] = {}
        for tag in all_tags:
            tag_count[tag] = tag_count.get(tag, 0) + 1
        
        threshold = len(preferences) / 2
        common_tags = [tag for tag, count in tag_count.items() if count >= threshold]
        
        budgets = [pref.budget_constraint for pref in preferences if pref.budget_constraint > 0]
        min_budget = min(budgets) if budgets else 0
        
        return {
            "common_tags": common_tags,
            "min_budget": min_budget,
            "participant_count": len(preferences),
            "tag_distribution": tag_count
        }
