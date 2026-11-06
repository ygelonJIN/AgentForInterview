"""中文数字与金额的词法还原。

为什么不用短语枚举：``三千``/``一万五``/``一百二``/``十二万`` 是同一套语法
的组合，枚举短语只能覆盖写下来的那几种。这里按"位值相加"还原整段数字，
再用 ``find_amounts`` 把文本里的金额片段连单位一起切出来。
"""
from __future__ import annotations

import re
from typing import List, NamedTuple, Optional

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
    "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}
_CN_SMALL_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_BIG_UNIT = {"万": 10000, "萬": 10000, "亿": 100000000}

_CN_RUN = re.compile(r"[零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬]+")

# 金额：数字 + 可选"万/千/k/w" + 可选货币单位；货币符号允许出现在前面
_MONEY = re.compile(
    r"(?P<prefix>[¥￥])?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>万|萬|千|仟|[kK](?![a-zA-Z])|[wW](?![a-zA-Z]))?"
    r"\s*(?P<cur>元|块|人民币|RMB|rmb)?"
)


class Amount(NamedTuple):
    """文本里的一段金额。``value`` 已归一化为元。"""

    value: float
    start: int
    end: int
    raw: str
    has_unit: bool  # 带 万/千/k/w 或 元/块/￥，用来区分"金额"和"数量"


def parse_cn_number(text: str) -> Optional[float]:
    """把一段纯中文数字还原为数值；不是合法数字返回 None。

    支持组合与口语省略：三千五=3500、一万五=15000、十二万=120000、
    一百零五=105、十五=15、两万三=23000。
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    total = 0.0
    section = 0.0
    number = 0.0
    last_unit = 0.0
    seen_zero = False
    seen_any = False

    for char in raw:
        if char in _CN_DIGIT:
            digit = float(_CN_DIGIT[char])
            number = digit
            seen_zero = seen_zero or digit == 0
            seen_any = True
        elif char in _CN_SMALL_UNIT:
            unit = float(_CN_SMALL_UNIT[char])
            # "十五" 的十前面没有数字，按 1 个十算；"一百零五" 的零之后不算。
            if number == 0.0 and not seen_zero:
                number = 1.0
            section += number * unit
            number = 0.0
            last_unit = unit
            seen_any = True
        elif char in _CN_BIG_UNIT:
            unit = float(_CN_BIG_UNIT[char])
            section = (section + number) * unit
            total += section
            section = 0.0
            number = 0.0
            last_unit = unit
            seen_any = True
        else:
            return None

    if not seen_any:
        return None
    if number:
        # 口语省略：一万五 / 三千五 / 一百五 的最后一位落在大单位的下一位。
        if seen_zero or last_unit <= 1.0:
            total += section + number
        else:
            total += section + number * (last_unit / 10.0)
    else:
        total += section
    return total


def _scale(num: float, unit: Optional[str]) -> float:
    if not unit:
        return num
    if unit in {"万", "萬"}:
        return num * 10000
    if unit in {"千", "仟", "k", "K"}:
        return num * 1000
    if unit in {"w", "W"}:
        return num * 10000
    return num


def find_amounts(text: str) -> List[Amount]:
    """切出文本里所有带单位/货币的金额片段（按出现顺序）。

    纯数字不带单位时不算金额——"5 天""3 人"里的 5 和 3 不是钱。
    """
    source = str(text or "")
    found: List[Amount] = []

    for match in _MONEY.finditer(source):
        raw_num = match.group("num")
        unit = match.group("unit")
        cur = match.group("cur")
        prefix = match.group("prefix")
        number = float(raw_num.replace(",", ""))
        value = _scale(number, unit)
        end = match.end()

        # "1万5" 的口语写法：万后面的单个数字是千位。
        if unit in {"万", "萬"} and match.group("unit"):
            tail = re.match(r"(\d)", source[end:end + 1])
            if tail:
                value += float(tail.group(1)) * 1000
                end += 1
        elif unit in {"千", "仟"}:
            tail = re.match(r"(\d)", source[end:end + 1])
            if tail:
                value += float(tail.group(1)) * 100
                end += 1

        has_unit = bool(unit or cur or prefix)
        if not has_unit:
            continue
        found.append(Amount(value, match.start(), end, source[match.start():end], True))

    if found:
        return found

    # 没有阿拉伯金额时，再看中文数字 + 货币单位（一万块 / 三千元）。
    for run in _CN_RUN.finditer(source):
        value = parse_cn_number(run.group(0))
        if value is None:
            continue
        tail = re.match(r"\s*(元|块|人民币)", source[run.end():run.end() + 4])
        if not tail:
            continue
        found.append(
            Amount(value, run.start(), run.end() + tail.end(), source[run.start():run.end() + tail.end()], True)
        )
    return found


def cn_or_arabic(token: str) -> Optional[float]:
    """把一个可能中英混排的数字片段转成数值（"3" / "三" / "两"）。"""
    token = str(token or "").strip()
    if not token:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    return parse_cn_number(token)
