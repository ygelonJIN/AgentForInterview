"""图状态定义。

顶层图和子图都只读写这些字段；控制流由边决定，不再由对象方法决定。
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict


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

    # 记忆
    memory_extraction: Dict[str, Any]
    # 按需提取时由 UI 传入的消息窗口（游标之后、已截断）
    memory_window: List[Dict[str, str]]

    # 点名调用可以指定 Agent，跳过分类兜底
    forced_agent: str

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


class GeneralState(TypedDict, total=False):
    """通用 Agent：ReAct 工具循环的状态（原 ReactState，改名后与 agent_id 对齐）。"""

    user_message: str
    thread_id: str
    history: List[Any]
    messages: List[Any]
    tool_results: List[Dict[str, Any]]
    step_count: int
    max_steps: int
    tool_timeout: float
    trace_id: str
    response: str
    status: str
    error: str
