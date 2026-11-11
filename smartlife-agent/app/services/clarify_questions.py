"""追问卡片的问题与选项。

一次问多个问题，每题给 A/B/C 三个可直接点的选项，再加一个"自己填"。
选项值是能直接被槽位解析的写法（"3 天"/"2 人"/"5000 元"），
用户点完拼成一句话回给图，不需要再走一次自然语言理解。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.nlu import lexicon

DAY_OPTIONS = (2, 3, 5, 7)
PEOPLE_OPTIONS = (1, 2, 3, 4)
BUDGET_OPTIONS = (2000, 3000, 5000, 8000)
CITY_FALLBACK = ("杭州", "成都", "西安")

_ANSWER_PREFIX = {
    "destination": "目的地",
    "cities": "目的地",
    "days": "天数",
    "people": "人数",
    "budget": "预算",
    "start_date": "出发日期",
    "budget_split": "预算分配",
}

_TITLES = {
    "destination": "去哪儿？",
    "days": "玩几天？",
    "people": "几个人？",
    "budget": "预算大概多少？",
    "start_date": "什么时候出发？",
}


def _option(label: str, value: Optional[str] = None) -> Dict[str, str]:
    return {"label": label, "value": value or label}


def _question(key: str, title: str, options: Sequence[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "answer_prefix": _ANSWER_PREFIX.get(key, title),
        "options": list(options),
        "allow_custom": True,
    }


def date_options(today=None) -> List[Dict[str, str]]:
    """出发日期给三个相对写法；解析交给 parse_start_date，不在这里算死。"""
    return [
        _option("明天"),
        _option("后天"),
        _option("下个月"),
    ]


# 人数 / 出发日期不在这里配选项：下面有专门的两块（key=people / start_date），
# 两处都配会让同一件事问两遍。这里只留给"校验后才冒出来"的那几类。
_VERIFY_OPTIONS = {
    "温标": ("-5℃", "0℃", "10℃"),
    "预算口径": ("含装备", "不含装备"),
}


def budget_split_questions(
    budget: float,
    estimate: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """预算到位后问一次"行程和装备各拿多少"，用滑动条。

    0 和 100 是两个端点：0 = 只买装备（走 shopping），100 = 只排行程（走 travel），
    中间才是 trip_gear。滑条比三个选项更能表达"偏一点"这种诉求。

    ``estimate`` 是机票+住宿的估算；界面用它提示"行程额度还不够覆盖这两块"，
    免得用户选完 90% 才发现机票住宿已经超过行程额度。
    """
    question = {
        "key": "budget_split",
        "type": "slider",
        # 标题要跟"预算多少"区分开：金额已经记下了，这里只问比例
        "title": f"行程和装备怎么分这 {float(budget):g} 元？",
        "answer_prefix": "预算分配",
        "budget": float(budget),
        "min": 0,
        "max": 100,
        "step": 10,
        "default": 80,
        "hint": "只定比例，不改金额：0 = 只买商品（装备），100 = 只排行程",
        "options": [],
        "allow_custom": False,
    }
    if estimate is not None:
        question["estimate"] = round(float(estimate), 2)
    return [question]


def build_questions(
    missing: Sequence[str],
    facts: Any = None,
    today: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """按缺失项生成问题；缺失项在前，可选补充项在后。"""
    questions: List[Dict[str, Any]] = []
    have = {str(item) for item in missing}

    if "目的地" in have:
        questions.append(_question("destination", _TITLES["destination"],
                                   [_option(city) for city in CITY_FALLBACK]))

    if "具体城市" in have:
        province = getattr(facts, "destination", "") or ""
        cities = list(lexicon.PROVINCE_CITIES.get(province, ()))
        questions.append(
            _question(
                "cities",
                f"{province}去哪几个城市？",
                [_option(city) for city in cities] or [_option(c) for c in CITY_FALLBACK],
            )
        )

    if "天数" in have:
        questions.append(_question("days", _TITLES["days"],
                                   [_option(f"{days} 天") for days in DAY_OPTIONS]))

    # 校验后追问的几类（温标、预算口径）也给选项；认不出的只留"自己填"
    for name in missing:
        for keyword, options in _VERIFY_OPTIONS.items():
            if keyword in name and not questions_has(questions, name):
                questions.append(_question(name, name, [_option(v) for v in options]))

    # 以下三项不是"卡住"的缺失，但这一轮已经在问了，顺手一起问
    if getattr(facts, "people", None) is None and not questions_has(questions, "people"):
        questions.append(_question("people", _TITLES["people"],
                                   [_option(f"{people} 人") for people in PEOPLE_OPTIONS]))
    if getattr(facts, "budget", None) is None and not questions_has(questions, "budget"):
        questions.append(_question("budget", _TITLES["budget"],
                                   [_option(f"{budget} 元") for budget in BUDGET_OPTIONS]))
    if not getattr(facts, "start_date", None) and not questions_has(questions, "start_date"):
        questions.append(_question("start_date", _TITLES["start_date"], date_options(today)))

    return questions


def questions_has(questions: Sequence[Dict[str, Any]], key: str) -> bool:
    return any(item.get("key") == key for item in questions)


def compose_answer(answers: Sequence[Dict[str, str]]) -> str:
    """把 {"prefix": "天数", "value": "3 天"} 拼成一句能解析的话。"""
    parts = [
        f"{item.get('prefix', '').strip()}：{str(item.get('value', '')).strip()}"
        for item in answers
        if str(item.get("value", "")).strip()
    ]
    return "；".join(parts)
