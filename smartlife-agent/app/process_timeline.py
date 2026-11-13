"""四阶段过程时间线，独立于旧模块缓存。"""
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
        "name": "记忆提取（按需）",
        "description": "回答结束后不自动判断；需要时在对话区点「提取记忆」，只提取上次之后的新消息",
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


LEGACY_TOTAL = 4

# 图自己声明的阶段（`step` 字段就是节点名）。有它们时按"链路的真实阶段"渲染，
# 不补 4 步模板——travel 的 total 也是 4，只凭 total 会和旧四阶段判成同一件事，
# 于是 travel 的 01/04 后面跟着"资料检索与准备 / 生成并审核回复"这些没发生过的阶段。
_CHAIN_STEP_NODES = {
    "derive", "clarify", "budget_split", "plan", "gear", "owned", "shop",
    "verify", "verify_clarify", "repair", "repair_confirm", "finalize",
}


def _step_events(source_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按发生顺序取步骤事件，同一阶段重复上报只留最后一次。

    顺序即事件流顺序：trip_gear 的 7 个阶段按图执行顺序发出，候选记忆确认
    在 finalize 之后才发，所以它天然排在最后，不会被当成第 4 阶段的重复。
    """
    ordered: List[Dict[str, Any]] = []
    index_by_key: Dict[tuple, int] = {}
    for event in source_events:
        if event.get("event") != "step":
            continue
        data = event.get("data") or {}
        try:
            step_number = int(data.get("step", 0))
        except (TypeError, ValueError):
            step_number = 0
        if step_number <= 0:
            continue
        key = (step_number, str(data.get("source") or ""), str(data.get("name") or ""))
        if key in index_by_key:
            ordered[index_by_key[key]] = data
        else:
            index_by_key[key] = len(ordered)
            ordered.append(data)
    return ordered


def build_process_timeline(
    events: Optional[List[Dict[str, Any]]],
    *,
    active_step: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """将步骤状态和细节事件整理成稳定顺序。

    两种链路：
    - 图声明了多阶段（事件里的 ``total`` 大于 4，如 trip_gear 的 7 步）：只输出
      真实发生过的阶段，编号、名称、状态、顺序都取事件本身，不补阶段、不猜状态；
    - 传统 4 阶段：沿用 ``DEFAULT_STEPS``，缺状态时按 ``active_step`` 推断。
    """
    source_events = [event for event in (events or []) if isinstance(event, dict)]
    step_updates: Dict[int, Dict[str, Any]] = {}
    details: Dict[int, List[Dict[str, Any]]] = {1: [], 2: [], 3: [], 4: []}
    unmatched: List[Dict[str, Any]] = []

    step_stream = _step_events(source_events)
    declared_total = 0
    for data in step_stream:
        try:
            declared_total = max(declared_total, int(data.get("total") or 0))
        except (TypeError, ValueError):
            continue

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

    chain_declared = any(
        str(event.get("step") or "") in _CHAIN_STEP_NODES
        for event in source_events
        if event.get("event") == "step"
    )
    if step_stream and (chain_declared or declared_total > LEGACY_TOTAL):
        # 多阶段链路按**原始顺序**输出：step 事件只留每个阶段最后一次（开始时的
        # active 会被结尾的 completed 覆盖），其余事件（classify / tool_call /
        # error / plan_result）原样保留。
        #
        # 以前这里只输出 step 列表、把 details 整个丢掉：`classify` 事件按
        # `_STEP_ALIASES` 归到第 1 阶段，于是**行程链路上根本看不到"意图与检索"
        # 那张卡片**——选了 RAG、命中了哪些关键词全都消失，只剩阶段名。
        # 图自己的阶段存在时，分类那一步（total=4、来源 classify）不单独占一行：
        # 它的编号是另一套（4 步 vs 8 步），并排会出现两个"01"；而它的全部信息
        # 都在下面那张"意图与检索"卡片里（选了哪条检索、命中哪些关键词）。
        has_graph_steps = any(
            str(event.get("step") or "") not in {"", "classify"}
            for event in source_events
            if event.get("event") == "step"
        )

        placed: Dict[tuple, int] = {}
        timeline: List[Dict[str, Any]] = []
        for event in source_events:
            if event.get("event") != "step":
                timeline.append(event)
                continue
            if has_graph_steps and str(event.get("step") or "") == "classify":
                continue
            data = dict(event.get("data") or {})
            data["status"] = data.get("status") or "completed"
            key = (
                data.get("step"),
                str(event.get("step") or ""),
                str(data.get("name") or ""),
            )
            if key in placed:
                # 同一阶段又跑了一遍（repair 之后的 verify、追问后的 derive）：
                # 原地更新描述与状态，不新增一行，也不把它挪到列表末尾。
                timeline[placed[key]] = {"event": "step", "data": data}
            else:
                placed[key] = len(timeline)
                timeline.append({"event": "step", "data": data})
        return timeline

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
