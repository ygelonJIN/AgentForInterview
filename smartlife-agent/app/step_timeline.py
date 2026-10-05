"""四阶段过程时间线，保证步骤卡从请求开始即可预期。"""
from typing import Any, Dict, List, Optional


DEFAULT_STEPS = {
    1: {
        "step": 1,
        "total": 4,
        "name": "统一分类",
        "description": "分析任务类型、执行路线和数据需求",
    },
    2: {
        "step": 2,
        "total": 4,
        "name": "资料检索与准备",
        "description": "准备任务执行所需的资料、历史和偏好上下文",
    },
    3: {
        "step": 3,
        "total": 4,
        "name": "生成并审核回复",
        "description": "生成回答，检查预算、天数、路线和事实；仅硬错误时修订一次",
    },
    4: {
        "step": 4,
        "total": 4,
        "name": "候选记忆确认",
        "description": "检查回答中是否有值得写入长期记忆的用户偏好",
    },
}

_STEP_ALIASES = {
    "classify": 1,
    "retrieval": 2,
    "generate": 3,
    "plan": 3,
    "execute": 3,
    "reflect": 3,
    "revise": 3,
    "memory": 4,
    "post_process": 4,
}


def _detail_step(event: Dict[str, Any]) -> int:
    return _STEP_ALIASES.get(str(event.get("step", "")), 0)


def infer_active_step(events: Optional[List[Dict[str, Any]]]) -> int:
    """根据已发生事件推断当前阶段，避免依赖旧 Agent 的 STEP 事件。"""
    active_step = 1
    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event", ""))
        data = event.get("data") or {}
        step_name = str(event.get("step", ""))
        if event_type == "memory_extraction" or data.get("step") == 4:
            return 4
        if (
            event_type in {"token", "response_reset"}
            or data.get("step") == 3
            or step_name in {"generate", "plan", "execute", "reflect", "revise"}
        ):
            active_step = max(active_step, 3)
        elif event_type == "classify" or data.get("step") == 2 or step_name == "retrieval":
            active_step = max(active_step, 2)
    return active_step


def build_process_timeline(
    events: Optional[List[Dict[str, Any]]],
    *,
    active_step: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """将步骤状态和细节事件整理成 Step 1-4 的稳定顺序。"""
    source_events = [event for event in (events or []) if isinstance(event, dict)]
    step_updates: Dict[int, Dict[str, Any]] = {}
    details: Dict[int, List[Dict[str, Any]]] = {1: [], 2: [], 3: [], 4: []}
    unmatched: List[Dict[str, Any]] = []

    for event in source_events:
        if event.get("event") == "step":
            data = event.get("data") or {}
            try:
                step_number = int(data.get("step", 0))
            except (TypeError, ValueError):
                step_number = 0
            if step_number in DEFAULT_STEPS:
                step_updates[step_number] = data
            continue
        target_step = _detail_step(event)
        if target_step:
            details[target_step].append(event)
        else:
            unmatched.append(event)

    if active_step is None:
        active_step = max(step_updates, default=1)
    active_step = min(max(int(active_step), 1), 4)

    timeline: List[Dict[str, Any]] = []
    for step_number in range(1, 5):
        data = dict(DEFAULT_STEPS[step_number])
        data.update(step_updates.get(step_number, {}))
        if not data.get("status"):
            if step_number < active_step:
                data["status"] = "completed"
            elif step_number == active_step:
                data["status"] = "active"
            else:
                data["status"] = "pending"
        timeline.append({"event": "step", "data": data})
        timeline.extend(details[step_number])

    timeline.extend(unmatched)
    return timeline
