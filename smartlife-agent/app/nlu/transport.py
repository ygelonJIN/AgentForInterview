"""交通方式码与中文标签的唯一映射。

放在 ``nlu`` 层而不是 ``trip_gear``：共享约束（``services/task_models``）和
行程事实（``trip_gear``）都要用，而 ``trip_gear`` 包初始化会反向导入
``services``——把映射放在任何一边都会绕成循环导入。这里只依赖标准库。

``undecided`` 是"用户明确说自己安排/还没定"，不是"没抽到"：前者不再阻塞
追问，也不再估算大交通；空串才表示还没问过。
"""
from __future__ import annotations

TRANSPORT_LABELS = {
    "flight": "飞机",
    "train": "高铁/火车",
    "drive": "自驾",
    "bus": "大巴/长途汽车",
    "ferry": "轮渡/邮轮",
    "mixed": "多种方式",
    "undecided": "未定（自己安排）",
}


def transport_label(mode: str) -> str:
    """交通方式码转人话；空串返回空串，未知码原样返回。"""
    value = str(mode or "")
    return TRANSPORT_LABELS.get(value, value)
