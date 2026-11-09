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
