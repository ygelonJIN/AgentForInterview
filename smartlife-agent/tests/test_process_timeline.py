"""过程时间线：多阶段链路要完整呈现，不能截断也不能伪造状态。"""

from app.process_timeline import build_process_timeline

TRIP_STEPS = [
    ("derive", 1, "约束派生"),
    ("plan", 2, "逐日行程"),
    ("gear", 3, "装备推导"),
    ("owned", 4, "已有装备"),
    ("shop", 5, "缺口匹配"),
    ("verify", 6, "预算与时间校验"),
    ("repair", 7, "按预算调整"),
    ("finalize", 7, "整理结果"),
]


def _trip_events(extra_status=""):
    events = [
        {
            "event": "step",
            "step": node,
            "data": {
                "step": step,
                "total": 7,
                "name": name,
                "description": f"{name}完成",
                "status": "completed",
            },
        }
        for node, step, name in TRIP_STEPS
    ]
    if extra_status:
        events.append({
            "event": "step",
            "step": "post_process",
            "data": {
                "step": 4,
                "total": 4,
                "name": "候选记忆确认",
                "description": "本轮没有需要写入长期记忆的内容",
                "status": extra_status,
                "source": "post_process",
            },
        })
    return events


def _steps(timeline):
    return [event["data"] for event in timeline if event.get("event") == "step"]


def test_trip_gear_seven_phases_are_not_truncated():
    steps = _steps(build_process_timeline(_trip_events()))

    assert [step["name"] for step in steps] == [
        "约束派生", "逐日行程", "装备推导", "已有装备",
        "缺口匹配", "预算与时间校验", "按预算调整", "整理结果",
    ]
    assert [step["step"] for step in steps] == [1, 2, 3, 4, 5, 6, 7, 7]
    assert all(step["status"] == "completed" for step in steps)


def test_memory_step_is_appended_after_trip_phases_instead_of_overwriting_step_four():
    steps = _steps(build_process_timeline(_trip_events(extra_status="waiting")))

    assert steps[3]["name"] == "已有装备"
    assert steps[-1]["name"] == "候选记忆确认"
    assert steps[-1]["status"] == "waiting"


def test_duplicate_phase_reports_keep_last_status_without_duplicating_rows():
    events = _trip_events()
    events.append({
        "event": "step",
        "step": "verify",
        "data": {"step": 6, "total": 7, "name": "预算与时间校验",
                 "description": "修复后复检通过", "status": "completed"},
    })

    steps = _steps(build_process_timeline(events))

    assert [step["name"] for step in steps].count("预算与时间校验") == 1
    assert steps[5]["description"] == "修复后复检通过"


def test_legacy_four_phase_pipeline_keeps_its_own_names():
    events = [
        {"event": "step", "data": {"step": 1, "total": 4, "name": "统一分类", "status": "completed"}},
        {"event": "step", "data": {"step": 3, "total": 4, "name": "生成并审核回复", "status": "completed"}},
        {"event": "step", "data": {"step": 4, "total": 4, "name": "候选记忆确认", "status": "completed"}},
        {"event": "tool_call", "step": "retrieval", "data": {"tool": "rag"}},
    ]

    steps = _steps(build_process_timeline(events, active_step=4))

    assert [step["name"] for step in steps] == [
        "统一分类", "资料检索与准备", "生成并审核回复", "候选记忆确认",
    ]
    assert all(step["status"] == "completed" for step in steps)


def test_unfinished_legacy_turn_does_not_claim_later_phases_completed():
    events = [
        {"event": "classify", "step": "classify", "data": {"intent": "general"}},
        {"event": "thinking", "step": "generate", "data": {"message": "开始生成"}},
    ]

    steps = _steps(build_process_timeline(events, active_step=1))

    assert steps[0]["status"] == "active"
    assert steps[1]["status"] == "pending"
    assert steps[3]["status"] == "pending"
