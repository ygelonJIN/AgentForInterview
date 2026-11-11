"""入口语料回归：一句常见说法 → 一组人工写的期望槽位。

为什么要有这个文件：只修单条说法，下一句换个说法又崩。这里一次跑几十句，
改任何解析逻辑都能立刻看出有没有把别的说法弄坏。

期望值是**人工写的**，不是从被测代码里跑出来的——否则等于自己给自己出题。
"""

import json
from pathlib import Path

import pytest

from app.nlu.slots import extract_slots

CORPUS = Path(__file__).resolve().parents[1] / "data" / "evaluation" / "nlu_corpus.jsonl"
CASES = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("case", CASES, ids=[case["message"] for case in CASES])
def test_corpus_slots(case):
    slots = extract_slots(case["message"], today=case.get("today"))
    actual = slots.__dict__

    for key, expected in case["expect"].items():
        assert actual.get(key) == expected, f"{case['message']} 的 {key} 期望 {expected!r}，实际 {actual.get(key)!r}"


def test_price_before_unit_is_not_days_or_total_budget():
    """「酒店找300左右一晚」：300 是房价，不能算总预算，也不能把「一晚」当成 2 天。"""
    from app.nlu.slots import extract_slots

    slots = extract_slots(
        "上海，去广东，情侣，一周，5000块，要去很多城市，包含汕头，"
        "做一个规划，我有一个pocket3，酒店找300左右一晚"
    )

    assert slots.origin == "上海", "句首裸地名 + 逗号 + 出行动词要认成出发地"
    assert slots.destination == "广东"
    assert slots.cities == ["汕头"], "上海是出发地，不该进串联城市"
    assert slots.days == 7, "「一周」要读成 7 天"
    assert slots.people == 2
    assert slots.budget == 5000.0, "房价不能被当成整趟预算"
    assert slots.lodging_budget == 300.0
