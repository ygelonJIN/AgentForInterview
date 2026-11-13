"""追问卡片的问题与选项。

一次问多个问题，每题给 A/B/C 三个可直接点的选项，再加一个"自己填"。
选项值是能直接被槽位解析的写法（"3 天"/"2 人"/"5000 元"），
用户点完拼成一句话回给图，不需要再走一次自然语言理解。

**只有答案本身是集合的题才允许多选**（"广东去哪几个城市？"），
靠题目上的 ``multi`` 标记区分：天数、人数、预算、出发日期、预算比例
都只有一个值，多选等于让用户自相矛盾。多选答案按选项顺序用"、"拼成
一个值（``广州、深圳、珠海``），照旧能被 ``slots.parse_cities`` 解析，
下游不需要知道它来自多选。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.nlu import lexicon
from app.trip_gear import estimates

DAY_OPTIONS = (2, 3, 5, 7)
PEOPLE_OPTIONS = (1, 2, 3, 4)
BUDGET_OPTIONS = (2000, 3000, 5000, 8000)
CITY_FALLBACK = ("杭州", "成都", "西安")
# 出发城市只给几个常见选项省打字，选不到一律自己填；这里没有默认值。
ORIGIN_OPTIONS = ("上海", "北京", "广州", "深圳", "成都", "杭州")

# 交通只问方式：票价按 estimates 里披露的单价估算，不是报价。
TRANSPORT_OPTIONS = (
    ("坐高铁", "坐高铁"),
    ("坐飞机", "坐飞机"),
    ("自驾", "自驾"),
)

# 住宿只问预算，不问类型：默认就是酒店（两人一间），民宿/露营只在原话里
# 自己出现时才走那条路——"露营"在 `parse_lodging` 里先于酒店词命中。
# 选项值仍拼成能直接解析的一句话（`酒店，每晚500元` → lodging + lodging_budget）。
# 价格来自 estimates 的单价常量（唯一一处），不是这里另编一套。
HOTEL_ECONOMY = int(estimates.HOTEL_PER_ROOM_NIGHT)
HOTEL_COMFORT = int(estimates.HOTEL_COMFORT_PER_ROOM_NIGHT)
HOTEL_HIGH = int(estimates.HOTEL_HIGH_PER_ROOM_NIGHT)
LODGING_OPTIONS = (
    (f"经济型 ¥{HOTEL_ECONOMY}/晚", f"酒店，每晚{HOTEL_ECONOMY}元"),
    (f"舒适型 ¥{HOTEL_COMFORT}/晚", f"酒店，每晚{HOTEL_COMFORT}元"),
    (f"高档 ¥{HOTEL_HIGH}/晚", f"酒店，每晚{HOTEL_HIGH}元"),
)

_ANSWER_PREFIX = {
    "origin": "出发地",
    "destination": "目的地",
    "transport": "交通",
    "lodging": "住宿",
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


MULTI_SEPARATOR = "、"


def _question(
    key: str,
    title: str,
    options: Sequence[Dict[str, str]],
    *,
    multi: bool = False,
    hint: str = "",
) -> Dict[str, Any]:
    """一道题。``multi=True`` 才渲染成可多选，其余一律单选。

    多选只给"答案是集合"的题（要去的城市）；单选在界面上表现为药丸
    选项，点一个就替换前一个，用户不会误以为能选两个矛盾的答案。
    """
    return {
        "key": key,
        "title": title,
        "answer_prefix": _ANSWER_PREFIX.get(key, title),
        "options": list(options),
        "allow_custom": True,
        "multi": bool(multi),
        "hint": hint,
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
    """按缺失项生成问题；缺失项在前，可选补充项在后。

    卡片的阅读顺序按"怎么想这趟旅行"排：**从哪出发 → 去哪 → 几天 → 几个人 →
    什么时候 → 怎么去 → 住哪**。文案一律整句问话（"从哪个城市出发？"），
    选项写成一句能直接被槽位解析的话，点完不用再过一遍自然语言理解。
    """
    questions: List[Dict[str, Any]] = []
    have = {str(item) for item in missing}

    if "出发地" in have:
        # 出发地决定大交通（机票/高铁段数）和第一天怎么走。给几个常见出发城市
        # 只是省打字，选不到就自己填；这里绝不给默认值。
        questions.append(
            _question(
                "origin",
                "从哪个城市出发？",
                [_option(city) for city in ORIGIN_OPTIONS],
                hint="大交通（机票/高铁）和第一天的路线按这个城市算",
            )
        )

    if "目的地" in have:
        questions.append(_question("destination", _TITLES["destination"],
                                   [_option(city) for city in CITY_FALLBACK]))

    if "具体城市" in have:
        province = getattr(facts, "destination", "") or ""
        cities = list(lexicon.PROVINCE_CITIES.get(province, ()))
        # 这一题的唯一正解就是一个集合：一天一个城市，串几个城市是行程的形状。
        # 单选只能拿一个城市，"广东玩一周"就退化成单城；所以这题必须多选。
        questions.append(
            _question(
                "cities",
                f"{province}去哪几个城市？",
                [_option(city) for city in cities] or [_option(c) for c in CITY_FALLBACK],
                multi=True,
                hint="可多选：按选中的顺序串联，选不到就自己填（用「、」隔开）",
            )
        )

    if "天数" in have:
        questions.append(_question("days", _TITLES["days"],
                                   [_option(f"{days} 天") for days in DAY_OPTIONS]))

    # 人数和出发日期在这里显式排进来：它们同样是阻塞项，位置应当跟在"天数"后面，
    # 而不是掉到最后那个"顺手一起问"的尾巴上（卡片顺序 = 用户想这趟旅行的顺序）。
    if "人数" in have:
        questions.append(_question("people", _TITLES["people"],
                                   [_option(f"{people} 人") for people in PEOPLE_OPTIONS]))
    if "出发日期" in have:
        questions.append(_question("start_date", _TITLES["start_date"], date_options(today)))

    if "交通" in have:
        # 只问方式，不问票价：票价按披露的单价估算（机票 ¥800/段、高铁 ¥120/段），
        # 面板与正文都写"估算"。用户要说具体票价，走"自己填"（"机票 1200）"）。
        questions.append(
            _question(
                "transport",
                "怎么去？",
                [_option(label, value) for label, value in TRANSPORT_OPTIONS],
                hint="按方式估大交通，票价是估算，不是报价",
            )
        )

    if "住宿" in have or "每晚住宿预算" in have:
        # 住宿一题把"类型 + 每晚价"一起答完：分成两题会让人先选类型再看价格，
        # 而且露营那一档根本不需要每晚价。选项值写成能被槽位解析的一句话。
        options = [_option(label, value) for label, value in LODGING_OPTIONS]
        questions.append(
            _question(
                "lodging",
                "住宿每晚预算多少？",
                options,
                hint="按酒店、两人一间估，直接算进总预算；选不到就自己填一个每晚价",
            )
        )

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


def option_values(question: Dict[str, Any]) -> Dict[str, str]:
    """标签 → 值。

    界面上点的是**标签**（"经济型 ¥300/晚"），回给图的是**值**
    （"酒店，每晚300元"）。两者相同的时候没人在意，一旦不同就必须显式翻译：
    直接把标签拼进答案，槽位解析不出来，卡片会一轮一轮问同一件事。
    """
    return {
        str(option.get("label")): str(option.get("value") or option.get("label"))
        for option in (question.get("options") or [])
    }


def questions_has(questions: Sequence[Dict[str, Any]], key: str) -> bool:
    return any(item.get("key") == key for item in questions)


def value_text(value: Any) -> str:
    """答案值 → 一句能解析的文本；多选答案按顺序拼成一个值。

    多选在界面上是列表，进图之前必须塌成一个"值"，否则下游（拼句、
    槽位解析、约束清单）都要多认一种类型。分隔符选"、"，因为
    ``slots.parse_cities`` 本来就按地名表扫描，不需要额外规则。
    """
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value]
        return MULTI_SEPARATOR.join(part for part in parts if part)
    return str(value or "").strip()


def compose_answer(answers: Sequence[Dict[str, Any]]) -> str:
    """把 {"prefix": "天数", "value": "3 天"} 拼成一句能解析的话。"""
    parts = [
        f"{str(item.get('prefix', '') or '').strip()}：{value_text(item.get('value'))}"
        for item in answers
        if value_text(item.get("value"))
    ]
    return "；".join(parts)
