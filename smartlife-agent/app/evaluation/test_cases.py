"""
评测测试用例
"""
from typing import List, Dict, Any


TEST_CASES = {
    "shopping": [
        {
            "question": "帮我找500块以内的防水跑步鞋",
            "expected_answer_contains": ["跑步鞋", "防水"],
            "expected_sql_conditions": ["price", "waterproof"],
            "category": "nl2sql",
        },
        {
            "question": "有没有耐磨的登山鞋推荐",
            "expected_answer_contains": ["登山鞋"],
            "expected_rag_keywords": ["耐磨"],
            "category": "rag",
        },
        {
            "question": "我想买个帐篷，预算600以内",
            "expected_answer_contains": ["帐篷"],
            "expected_sql_conditions": ["price", "category"],
            "category": "nl2sql",
        },
        {
            "question": "帮我查一下我之前的订单",
            "expected_answer_contains": ["订单"],
            "category": "customer_service",
        },
        {
            "question": "这个跑步鞋的评价怎么样",
            "expected_answer_contains": ["评价", "跑步鞋"],
            "category": "rag",
        },
    ],
    "travel": [
        {
            "question": "我和女朋友周末去杭州玩两天，预算3000",
            "expected_answer_contains": ["杭州", "行程"],
            "expected_plan_steps": 4,
            "category": "plan_execute",
        },
        {
            "question": "推荐适合亲子的户外活动",
            "expected_answer_contains": ["亲子", "活动"],
            "category": "recommendation",
        },
        {
            "question": "三亚5天4晚大概要多少钱",
            "expected_answer_contains": ["三亚", "费用"],
            "category": "planning",
        },
    ],
    "cross_scene": [
        {
            "question": "我上周买了帐篷和登山杖，周末去哪玩好",
            "expected_answer_contains": ["露营"],
            "expected_memory_type": "cross_scene",
            "category": "memory",
        },
    ],
}


def get_test_cases(category: str = None) -> List[Dict[str, Any]]:
    """获取测试用例"""
    if category:
        return TEST_CASES.get(category, [])
    all_cases: List[Dict[str, Any]] = []
    for cases in TEST_CASES.values():
        all_cases.extend(cases)
    return all_cases
