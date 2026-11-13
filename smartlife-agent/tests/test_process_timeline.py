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


def test_trip_chain_keeps_the_classification_card_but_not_its_own_step_row():
    """行程链路（8 步）里也要能看到"意图与检索"卡片。

    回归背景：多阶段分支以前只输出 step 列表，`classify` 事件按别名归到第 1 阶段
    后被整块丢掉，于是整个过程区只剩阶段名——选了 RAG、命中了哪些关键词都看不见。
    """
    events = [
        {"event": "step", "step": "classify",
         "data": {"step": 1, "total": 4, "name": "统一分类",
                  "description": "关键词直通（强证据） · 检索 RAG", "status": "completed"}},
        {"event": "classify", "step": "classify",
         "data": {"intent": "travel", "retrieval": "rag_only", "method": "keyword",
                  "keyword_hits": {"travel": ["广州"]}}},
        *_trip_events(),
    ]

    timeline = build_process_timeline(events, active_step=4)
    kinds = [event["event"] for event in timeline]

    assert "classify" in kinds, "分类卡片不能被丢掉"
    # 分类那一步的编号属于另一套（4 步），不和图自己的 8 步并排显示两个 01
    assert [step["name"] for step in _steps(timeline)].count("统一分类") == 0
    assert _steps(timeline)[0]["name"] == "约束派生"


def test_storage_keeps_process_events_even_when_the_turn_streamed_a_lot():
    """一轮回答有上千条 token 时，入库也不能把 step/classify 丢掉。

    回归背景：`_turn_events_for_storage` 先看体积、超了只留 `plan_result`。
    token 是逐字一条，一轮 5 天行程轻易几百 KB，于是历史回看只剩默认四行
    ——"追问之后过程全没了"就是这么来的。
    """
    from app.streaming import turn_events_for_storage

    steps = [
        {"event": "classify", "step": "classify",
         "data": {"intent": "travel", "retrieval": "rag_only"}},
        {"event": "step", "step": "derive",
         "data": {"step": 1, "total": 4, "name": "约束派生",
                  "description": "已派生 6 条约束", "status": "completed"}},
        {"event": "step", "step": "plan",
         "data": {"step": 2, "total": 4, "name": "逐日行程",
                  "description": "已生成 5 天行程（广州）", "status": "completed"}},
    ]
    tokens = [
        {"event": "token", "step": "generate", "data": {"token": "字" * 200}}
        for _ in range(2000)
    ]

    kept = turn_events_for_storage(steps[:1] + tokens[:1000] + steps[1:] + tokens[1000:])

    assert [event["event"] for event in kept] == ["classify", "step", "step"]
    assert not any(event["event"] == "token" for event in kept)


def test_storage_falls_back_to_plan_result_only_when_still_too_big():
    from app.streaming import turn_events_for_storage

    events = [
        {"event": "step", "step": "derive",
         "data": {"step": 1, "total": 4, "name": "约束派生", "description": "x" * 4000}},
        {"event": "plan_result", "data": {"facts": {}, "constraints": []}},
    ]

    kept = turn_events_for_storage(events, max_bytes=500)

    assert [event["event"] for event in kept] == ["plan_result"]


def test_stale_clarify_from_a_previous_leg_is_not_counted_again():
    """续跑时只看新事件：铺回去的旧 clarify 不能再被当成"这次又中断了"。

    回归背景：`run_streaming_realtime` 返回的是"上一段 + 这一段"的合并列表，
    界面在整份列表里找 clarify，于是每次续跑都命中上一段那条旧事件——
    同一句追问重复输出、追问卡片赖着不走、过程区永远不收起。
    """
    from app.streaming import find_clarify_event

    seed = [
        {"event": "step", "data": {"step": 1, "total": 4, "name": "约束派生", "status": "waiting"}},
        {"event": "clarify", "step": "clarify", "data": {"question": "还需要补充：出发地"}},
    ]
    new_leg = [
        {"event": "step", "data": {"step": 1, "total": 4, "name": "约束派生", "status": "completed"}},
        {"event": "step", "data": {"step": 2, "total": 4, "name": "逐日行程", "status": "completed"}},
    ]

    assert find_clarify_event(seed + new_leg, since=len(seed)) is None
    # 这一段真的又中断了：能正常找到
    again = [{"event": "clarify", "data": {"question": "预算怎么分？"}}]
    found = find_clarify_event(seed + new_leg + again, since=len(seed))
    assert found is not None and found["data"]["question"] == "预算怎么分？"
