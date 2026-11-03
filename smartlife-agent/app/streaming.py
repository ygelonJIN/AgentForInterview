"""事件类型定义与过程事件整理。

真正的流式由 LangGraph 承担（``app/graphs/events.py`` 的 ``EventSink``）；
这里只保留事件枚举，以及 UI 侧对过程事件去重/合并的纯函数。
"""
from typing import Any, Dict, List
from enum import Enum


def _event_type_value(event_type: Any) -> str:
    return str(getattr(event_type, "value", event_type))


class EventType(str, Enum):
    """事件类型枚举"""
    THINKING = "thinking"          # 模型正在思考
    CLASSIFY = "classify"          # 分类结果（路由+意图合并）
    ROUTE = "route"                # 路由结果（兼容旧接口）
    INTENT = "intent"              # 意图识别结果
    TOOL_CALL = "tool_call"        # 工具调用开始
    TOOL_RESULT = "tool_result"    # 工具返回结果
    RETRIEVAL = "retrieval"        # 检索结果
    TOKEN = "token"                # 流式 token（打字机效果）
    RESPONSE_RESET = "response_reset"  # 新版本正文替换旧版本正文
    STEP = "step"                  # 步骤进度
    EXECUTION_LOG = "execution_log"  # 分支、重试、循环、降级日志
    ERROR = "error"                # 错误
    DONE = "done"                  # 完成


def deduplicate_process_events(events: Any) -> List[Dict[str, Any]]:
    """按逻辑执行节点去重，同时保留该节点最后一次状态。

    流式 UI 会随每个事件刷新整块过程区，后端重试或重复事件不应显示成两次执行。
    Step 按步骤编号去重，分类结果按分类节点去重，工具事件只合并完全相同的调用。
    """
    ordered_keys: List[Any] = []
    by_key: Dict[Any, Dict[str, Any]] = {}

    for index, event in enumerate(events or []):
        if not isinstance(event, dict):
            continue
        event_type = _event_type_value(event.get("event", ""))
        data = event.get("data") or {}
        stream_step = event.get("step", "")

        if event_type == EventType.STEP:
            logical_step = data.get("step") or data.get("name") or stream_step
            key = ("step", logical_step)
        elif event_type == EventType.CLASSIFY:
            key = ("classify", stream_step or "classify")
        elif event_type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
            payload_key = "input" if event_type == EventType.TOOL_CALL else "output"
            key = (
                event_type,
                stream_step,
                data.get("tool", ""),
                payload_key,
                data.get(payload_key, ""),
            )
        else:
            key = ("unique", index)

        if key not in by_key:
            ordered_keys.append(key)
        by_key[key] = event

    return [by_key[key] for key in ordered_keys]


def reconcile_tool_events(
    events: Any,
    *,
    completed: bool = False,
) -> List[Dict[str, Any]]:
    """合并工具调用与结果，避免调用卡片永久停留在“正在执行”。

    同一工具可能连续执行多次，因此按 ``(tool, step)`` 的出现顺序配对。
    流式阶段保留未返回结果的 active 状态；当整个过程已完成时，即使旧事件流缺少
    ``tool_result``，也把遗留调用收尾为 completed。
    """
    reconciled: List[Optional[Dict[str, Any]]] = []
    pending: Dict[Any, List[int]] = {}

    for index, event in enumerate(events or []):
        if not isinstance(event, dict):
            continue
        normalized = dict(event)
        normalized["data"] = dict(event.get("data") or {})
        event_type = _event_type_value(normalized.get("event", ""))
        data = normalized["data"]
        key = (data.get("tool", ""), event.get("step", ""))

        if event_type == EventType.TOOL_CALL:
            pending.setdefault(key, []).append(len(reconciled))
            reconciled.append(normalized)
            continue

        if event_type == EventType.TOOL_RESULT:
            call_indexes = pending.get(key) or []
            if call_indexes:
                call_index = call_indexes.pop(0)
                call = reconciled[call_index]
                call["data"]["status"] = "completed"
                call["data"]["output"] = data.get("output", "")
            reconciled.append(normalized)
            continue

        reconciled.append(normalized)

    if completed:
        for call_indexes in pending.values():
            for call_index in call_indexes:
                reconciled[call_index]["data"]["status"] = "completed"

    return [event for event in reconciled if event is not None]
