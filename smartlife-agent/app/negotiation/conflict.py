"""
冲突解决模块
"""
from typing import Dict, List, Any
from langchain_core.prompts import ChatPromptTemplate
from app.config import create_small_llm

class ConflictResolver:
    """冲突解决器"""
    
    def __init__(self, model_name: str = None):
        self.llm = create_small_llm()
        
        self.resolve_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个协商调解专家。多人出行/购物场景中，参与者有不同偏好和需求。

参与者偏好：{preferences}
冲突点：{conflicts}
共同点：{common_ground}

请给出妥协方案，要求：
1. 尽量满足每个人的核心需求
2. 找到大家都可接受的交集
3. 如果预算冲突，给出分级方案
4. 给出具体建议和理由

输出JSON格式：compromise_plan, satisfies, suggestions, budget_allocation"""),
            ("user", "请给出妥协方案。")
        ])
    
    def resolve(self, preferences: List[Dict], conflicts: List[Dict], common_ground: Dict) -> Dict[str, Any]:
        """解决冲突"""
        try:
            chain = self.resolve_prompt | self.llm
            result = chain.invoke({
                "preferences": str(preferences),
                "conflicts": str(conflicts),
                "common_ground": str(common_ground)
            })
            import json
            return json.loads(result.content)
        except Exception:
            return {"compromise_plan": "建议选择大家都感兴趣的活动，预算取中间值", "raw": True}
    
    def identify_conflicts(self, preferences: List[Dict]) -> List[Dict]:
        """识别冲突点"""
        if len(preferences) < 2:
            return []
        
        conflicts = []
        
        # 预算冲突
        budgets = [p.get("budget_constraint", 0) for p in preferences if p.get("budget_constraint", 0) > 0]
        if budgets and max(budgets) - min(budgets) > 500:
            conflicts.append({
                "type": "budget",
                "description": f"预算差异较大：{min(budgets)}-{max(budgets)}元",
                "severity": "high"
            })
        
        # 风格冲突
        all_styles: set = set()
        for p in preferences:
            styles = p.get("travel_preferences", {}).get("preferred_styles", [])
            all_styles.update(styles)
        
        if len(all_styles) > 3:
            conflicts.append({
                "type": "style",
                "description": f"旅行风格偏好差异：{', '.join(all_styles)}",
                "severity": "medium"
            })
        
        return conflicts
