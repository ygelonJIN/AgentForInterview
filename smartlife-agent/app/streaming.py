"""事件类型定义与过程事件整理。

真正的流式由 LangGraph 承担（``app/graphs/events.py`` 的 ``EventSink``）；
这里只保留事件枚举，以及 UI 侧对过程事件去重/合并的纯函数。
"""
from typing import Any, Dict, List
from enum import Enum


def _event_type_value(event_type: Any) -> str:
    return str(getattr(event_type, "value", event_type))


# 过程展示的"人话"词表。事件里存的是内部枚举（react / rag_only / llm_arbitration），
# 界面和过程卡片直接显示这些词等于让用户读代码。词表只放展示用文案，
# 不参与任何判定；分发逻辑仍看原始枚举。
ROUTE_LABELS = {"react": "单步", "plan_and_execute": "先规划"}
INTENT_LABELS = {
    "shopping": "购物",
    "travel": "旅行",
    "customer_service": "客服",
    "general": "通用",
}
RETRIEVAL_LABELS = {
    "sql_only": "SQL",
    "rag_only": "RAG",
    "mixed": "混合",
    "none": "无需检索",
}
RETRIEVAL_SCOPE = {
    "sql_only": "查商品属性",
    "rag_only": "查评价/攻略",
    "mixed": "商品属性 + 评价/攻略",
    "none": "纯对话",
}
# "这条链路是怎么定下来的"：关键词直通 / 沿用上一轮 / 小模型 / 主模型仲裁
CLASSIFY_METHOD_LABELS = {
    "keyword": "关键词直通（强证据）",
    "context": "沿用上一轮意图",
    "llm": "小模型判定",
    "llm_arbitration": "低置信 → 主模型仲裁",
    "llm_low_confidence": "仲裁失败，保留低置信结果",
    "llm_repair": "输出格式异常 → 主模型修复",
    "policy": "场景策略纠正",
    "fallback": "兜底默认值",
}


def describe_classification_method(method: Any) -> str:
    """分类来源的内部枚举 → 一句人话；认不出就原样显示，不吞掉证据。"""
    text = str(method or "").strip()
    return CLASSIFY_METHOD_LABELS.get(text, text or "未知")


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


# 入库前丢掉的事件：token 是逐字一条（一轮回答上千条、几十上百 KB），
# thinking 是瞬时的"正在干什么"，done/reset 只是控制信号。它们撑不起面板，
# 却足以把整份事件顶过体积上限，触发"只留 plan_result"的降级，
# 于是历史回看只剩四行默认阶段——过程"消失"的真正原因。
STORAGE_SKIP_KINDS = {"token", "thinking", "response_reset", "done", "memory_extraction"}
MAX_TURN_EVENT_BYTES = 256 * 1024


def turn_events_for_storage(
    events: Any,
    *,
    max_bytes: int = MAX_TURN_EVENT_BYTES,
) -> List[Dict[str, Any]]:
    """历史回看只需要能撑起面板的事件；先去噪，仍超量才退化成只留方案事件。"""
    kept = [
        dict(event)
        for event in (events or [])
        if isinstance(event, dict) and event.get("event") not in STORAGE_SKIP_KINDS
    ]
    if not kept:
        return []
    try:
        import json

        if len(json.dumps(kept, ensure_ascii=False)) <= max_bytes:
            return kept
    except (TypeError, ValueError):
        pass
    return [event for event in kept if event.get("event") == "plan_result"]


def find_clarify_event(events: Any, since: int = 0) -> Any:
    """在这一段**新产生**的事件里找中断事件。

    续跑时事件列表前面还铺着上一段（`since` 之前）的事件，里面本来就有那条旧的
    `clarify`。在整份列表里找，每次续跑都会把上次的追问当成"这次又问了"：
    同一句追问被重复输出、追问卡片赖着不走、过程区永远不收起。
    """
    for event in list(events or [])[max(0, int(since or 0)):]:
        if isinstance(event, dict) and event.get("event") == "clarify":
            return event
    return None


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
            # 同一编号下可能有多个真实阶段（trip_gear 的 repair/finalize 都是 7，
            # 候选记忆确认也在第 4 步），只按编号去重会把后一个阶段吃掉。
            key = ("step", logical_step, data.get("name", ""))
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
