"""图状态定义。

顶层图和子图都只读写这些字段；控制流由边决定，不再由对象方法决定。
"""
from __future__ import annotations

from typing import Annotated, Any, Dict, List, TypedDict


def branch_key(item: Dict[str, Any]) -> str:
    """分支结果的唯一键。

    ``BranchOutcome`` 的字段名是 ``name``，早期写法用 ``branch``；
    两边都认，避免再出现“结果都在、却按错键取不到”的问题。
    """
    if isinstance(item, dict):
        key = item.get("name") or item.get("branch")
        if key:
            return str(key)
    return str(id(item))


def merge_branch_results(left: List[Dict[str, Any]], right: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """并行分支结果合并；保持稳定顺序，重复分支名以后写入为准。"""
    merged: Dict[str, Dict[str, Any]] = {}
    for item in list(left or []) + list(right or []):
        merged[branch_key(item)] = item
    return list(merged.values())


class TurnState(TypedDict, total=False):
    """一轮对话的完整状态。"""

    # 输入
    user_message: str
    user_id: str
    session_id: str
    scene: str
    excluded_memories: List[str]
    # 图内消息序列：由 prepare 用 user_message 播种，供 react 等子图读取。
    messages: List[Any]

    # 分类与约束
    thread_id: str
    classification: Dict[str, Any]
    constraints: Dict[str, Any]
    route: str

    # 执行结果
    response: str
    agent_used: str
    branch_results: Annotated[List[Dict[str, Any]], merge_branch_results]

    # 记忆
    memory_extraction: Dict[str, Any]

    # 状态
    status: str
    error: str


class ShoppingState(TypedDict, total=False):
    user_message: str
    user_id: str
    thread_id: str
    classification: Dict[str, Any]
    needs: Dict[str, Any]
    planned_strategy: str
    retrieval: Dict[str, Any]
    constraints: Dict[str, Any]
    response: str
    status: str


class CustomerServiceState(TypedDict, total=False):
    user_message: str
    user_id: str
    thread_id: str
    history: List[Any]
    orders: List[Dict[str, Any]]
    response: str
    status: str


class ReactState(TypedDict, total=False):
    messages: List[Any]
    tool_results: List[Dict[str, Any]]
    step_count: int
    max_steps: int
    tool_timeout: float
    trace_id: str
    response: str
    status: str
    error: str


class GeneralState(TypedDict, total=False):
    user_message: str
    thread_id: str
    history: List[Any]
    response: str
    status: str
