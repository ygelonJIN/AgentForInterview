"""Agent 配置页：开关、工具面、输出样式。

这一页只做两件事：**启停**和**看清隔离**。
- 一个 Agent = 一个图节点 = 一条工具白名单；
- 停用后不接客，路由回退到通用助手；
- 每个 Agent 只能调用自己白名单里的工具，跨域调用在执行层被拒绝。

新增 Agent：写 `build_xxx_graph()` → 在 `app/agents/specs.py` 加一条 `AgentSpec`
→ 在 `app/tools/agent_capabilities.py` 加一条白名单。
"""

from __future__ import annotations

from typing import List

import streamlit as st

from app.agents import config as agent_config
from app.agents.specs import AGENT_SPECS
from app.tools.agent_capabilities import AGENT_TOOL_POLICIES, get_agent_tool_policy
from app.tools.custom import CUSTOM_DIR, custom_tool_names
from app.ui import theme

_OUTPUT_STYLE = {
    "shopping": "lead · stats · cards · table（正文进折叠原文）",
    "travel": "问答走 text 块；多天规划走 01–05 面板",
    "trip_gear": "01–05 面板（约束 / 行程 / 装备 / 预算 / 校验）",
    "general": "text 块",
    "customer_service": "lead · stats · timeline（正文进折叠原文）",
}

_ENTRY_HINT = {
    "shopping": "关键词：品类/品牌 + 购买动作",
    "travel": "关键词：地名、旅游词；或 travel + plan_trip",
    "trip_gear": "gear_plan，或复合意图，或出现露营/装备/清单",
    "general": "兜底：其它都没命中时进这里",
    "customer_service": "关键词：订单、退款、物流等（两个词起）",
}


def _tool_pool() -> List[str]:
    """全局工具池：所有 Agent 的默认白名单并集 + 用户自定义工具。"""
    names = {name for policy in AGENT_TOOL_POLICIES.values() for name in policy.tool_names}
    names |= set(custom_tool_names())
    return sorted(names)


def _tools_markdown(agent_id: str) -> str:
    policy = get_agent_tool_policy(agent_id)
    return " · ".join(f"`{name}`" for name in sorted(policy.tool_names))


def _render_agent(agent_id: str, enabled: bool) -> None:
    spec = AGENT_SPECS[agent_id]
    policy = AGENT_TOOL_POLICIES.get(agent_id)

    with st.container(border=True):
        head, switch = st.columns([5, 1])
        with head:
            st.markdown(f"#### {spec.display_name}  ·  `{agent_id}`")
            st.caption(spec.description)
        with switch:
            st.toggle(
                "启用",
                value=enabled,
                key=f"agent_enabled_{agent_id}",
                on_change=_flip,
                args=(agent_id,),
            )

        left, right = st.columns([3, 2])
        with left:
            st.markdown("**工具面**")
            st.markdown(_tools_markdown(agent_id))
            pool = _tool_pool()
            chosen = st.multiselect(
                "从全局工具池里选（保存后只注册选中的）",
                options=pool,
                # 配置里可能还留着已删掉的自定义工具名，别让默认值跑出选项
                default=[name for name in sorted(policy.tool_names) if name in pool],
                key=f"tools_{agent_id}",
                label_visibility="collapsed",
            )
            if st.button("保存工具", key=f"save_tools_{agent_id}"):
                agent_config.set_tools(agent_id, chosen)
                st.toast(f"{agent_id} 工具已更新（{len(chosen)} 个）")
                st.rerun()
            st.caption(
                f"当前 {len(policy.tool_names) if policy else 0} 个；"
                "未选中的不会注册进来，跨域调用在执行层也会被拒绝"
            )
        with right:
            st.markdown("**自然语言入口**")
            st.caption(_ENTRY_HINT.get(agent_id, f"意图 `{spec.intent}`"))
            st.markdown("**输出样式**")
            st.caption(_OUTPUT_STYLE.get(agent_id, "text 块"))


def _flip(agent_id: str) -> None:
    enabled = bool(st.session_state.get(f"agent_enabled_{agent_id}"))
    agent_config.set_enabled(agent_id, enabled)
    st.toast(f"{agent_id} 已{'启用' if enabled else '停用'}")


def render_agents_page() -> None:
    theme.render_page_header(
        "Agent 配置",
        "一个 Agent = 一个图节点 = 一条工具白名单。开关立即生效；停用后不接客，回退到通用助手。",
        "Agents",
    )

    enabled_map = agent_config.load_enabled()
    total_tools = sum(len(policy.tool_names) for policy in AGENT_TOOL_POLICIES.values())
    unique_tools = len({name for policy in AGENT_TOOL_POLICIES.values() for name in policy.tool_names})

    theme.metric_columns([
        ("Agent", str(len(AGENT_SPECS))),
        ("已启用", str(sum(1 for key in AGENT_SPECS if enabled_map.get(key, True)))),
        ("工具面合计", str(total_tools)),
        ("去重后工具", str(unique_tools)),
    ])

    st.caption(
        "工具是共享基础设施、能力面是隔离的：天气/路线这类事实工具多个 Agent 各自声明，"
        "商品、订单、时差这类领域工具只在对应 Agent 的白名单里。"
    )

    for agent_id in AGENT_SPECS:
        _render_agent(agent_id, enabled_map.get(agent_id, True))

    st.divider()
    if st.button("恢复默认（全部启用）", type="secondary"):
        agent_config.reset()
        for agent_id in AGENT_SPECS:
            st.session_state[f"agent_enabled_{agent_id}"] = True
        st.rerun()
