"""中文数字与金额的词法还原。"""

import pytest

from app.nlu.numerals import cn_or_arabic, find_amounts, parse_cn_number


@pytest.mark.parametrize(
    "text,expected",
    [
        ("三", 3),
        ("十", 10),
        ("十五", 15),
        ("二十三", 23),
        ("一百零五", 105),
        ("一百五", 150),
        ("两千", 2000),
        ("三千五", 3500),
        ("一万", 10000),
        ("一万五", 15000),
        ("一万二千", 12000),
        ("两万三", 23000),
        ("十二万", 120000),
        ("十万", 100000),
    ],
)
def test_chinese_numbers_are_composed_not_enumerated(text, expected):
    assert parse_cn_number(text) == expected


def test_invalid_chinese_number_returns_none():
    assert parse_cn_number("预算") is None
    assert parse_cn_number("") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3千", 3000),
        ("1.5万", 15000),
        ("1万5", 15000),
        ("2k", 2000),
        ("1.5w", 15000),
        ("800元", 800),
        ("¥1,000", 1000),
        ("一万块", 10000),
    ],
)
def test_find_amounts_normalizes_units(text, expected):
    amounts = find_amounts(text)
    assert amounts, text
    assert amounts[0].value == expected


def test_bare_numbers_are_not_amounts():
    assert find_amounts("去杭州玩5天，3个人") == []


def test_cn_or_arabic_accepts_both_forms():
    assert cn_or_arabic("3") == 3
    assert cn_or_arabic("三") == 3
    assert cn_or_arabic("") is None
