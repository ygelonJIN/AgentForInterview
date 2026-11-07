"""槽位抽取：一句话 → 目的地 / 天数 / 人数 / 预算 / 日期 / 住宿 / 交通。

这是**唯一**的槽位来源。``SharedConstraints``（跨场景共享）和 ``TripFacts``
（行程事实）都从这里取值，不再各写一套正则——之前同一条消息在两条链路上
抽出两个人数、两个目的地，就是这个原因。

所有解析都只做确定性规则，不调模型；需要"今天"的（明天、下周六、国庆）
由调用方注入，保证评测结果可复现。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Optional

from app.nlu import lexicon
from app.nlu.numerals import cn_or_arabic, find_amounts

_NUM = r"\d+|[零〇一二两三四五六七八九十百千]+"

# 目的地：动词后面跟 2~8 个汉字
_DEST_VERB = re.compile(r"(?:去|到|在|前往|想去|准备去|打算去|飞往|飞到|飞|坐高铁去|自驾去|回到|回)")
_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]{2,8}")
_LEADING_PARTICLES = re.compile(r"^(?:并|和|再|然后|顺便|规划|安排|去|到|在|回)+")
_PLACES_BY_LENGTH = tuple(sorted(lexicon.PLACES, key=len, reverse=True))
_PLACE_BEFORE_DURATION = re.compile(
    rf"([\u4e00-\u9fa5]{{2,6}}?)\s*(?:{_NUM})\s*(?:天|日|晚)"
)

# 人数
_PEOPLE_BIG_SMALL = re.compile(rf"(?P<big>{_NUM})\s*(?:个)?\s*(?:大人|成人)?\s*大\s*(?P<small>{_NUM})\s*(?:个)?\s*小")
_PEOPLE_FAMILY = re.compile(rf"一家\s*(?P<n>{_NUM})\s*口")
_PEOPLE_COUNT = re.compile(rf"(?P<n>{_NUM})\s*(?:个|名|位)?\s*(?:人|位|口)")
_PEOPLE_PAIR_WORDS = ("我俩", "我们俩", "两人", "二人世界", "情侣", "夫妻", "两口子", "小两口", "双人")
_PEOPLE_ALONE_WORDS = ("独自", "一个人", "单人", "自己去", "就我一个", "一个人去")
_PEOPLE_RELATION = re.compile(
    r"(?:我|我们)(?:和|跟|与|带)\s*(?:老婆|老公|媳妇|先生|太太|爱人|对象|女朋友|男朋友|朋友|闺蜜|家人|父母|爸妈|孩子|娃)"
)

# 天数
_DAYS = re.compile(rf"(?<![今明后昨前第月])(?P<n>{_NUM})\s*(?:天|日)")
_NIGHTS = re.compile(rf"(?P<n>{_NUM})\s*晚")
_WEEKS = re.compile(rf"(?P<n>{_NUM}|半)\s*个?\s*(?:周|星期|礼拜)")
_HALF_MONTH = re.compile(r"(?:半|0\.5)\s*个?\s*月")
_MONTHS = re.compile(rf"(?P<n>{_NUM})\s*个\s*月")

# 日期
_ISO_DATE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
_CN_DATE = re.compile(r"(?:(20\d{2})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_SLASH_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)")
_RELATIVE_DAYS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_WEEKDAY_WORDS = {
    "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6,
    "星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3, "星期五": 4, "星期六": 5, "星期日": 6,
    "礼拜一": 0, "礼拜二": 1, "礼拜三": 2, "礼拜四": 3, "礼拜五": 4, "礼拜六": 5, "礼拜日": 6,
}
_FIXED_HOLIDAYS = {
    "国庆": (10, 1), "十一": (10, 1), "五一": (5, 1), "劳动节": (5, 1), "元旦": (1, 1),
}

# 预算语境
_TOTAL_CUES = ("总预算", "全部预算", "整体预算", "总花费", "总花销", "总开销", "一共", "总共", "合计", "预算")
_MAX_CUES = ("不超过", "以内", "以下", "最多", "最高", "低于", "上限", "别超", "不要超过", "封顶", "控制在")
_MIN_CUES = ("以上", "至少", "不低于", "起步")
_GEAR_CUES = ("装备预算", "装备费用", "装备花费", "装备的钱", "装备钱", "装备另算", "装备单算", "装备单独算", "装备自己买")
# 提示词在前的写法。原先漏了"一共/总共/合计"——它们就在 _TOTAL_CUES 里，
# 却没有出现在这个正则里，于是"一共3000"抽不到预算。
_BARE_AMOUNT = re.compile(
    rf"(?:总预算|全部预算|整体预算|总花费|总花销|总开销|预算|"
    rf"一共|总共|合计|不超过|以内|最多|最高|低于|控制在)"
    rf"\D{{0,4}}(?P<num>\d+(?:\.\d+)?)"
)
# 数字在前的口语倒装："3000 预算" / "3000 块预算"。中文口音里这比"预算3000"更常见。
_BARE_BEFORE_BUDGET = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?:元|块|块钱)?\s*(?:的)?\s*(?:总)?预算"
)
_BARE_AFTER = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:以内|以下|左右|上下)")
# 中文数字要带上 万/亿：漏了它们，"预算一万"只会匹配到"一"（1 元），
# "一万预算"干脆匹配不到。
_CN_NUM = r"[零〇一二两三四五六七八九十百千万亿]+"
_BARE_CN = re.compile(
    rf"(?:总预算|预算|不超过|最多|最高|低于|控制在)\s*(?P<cn>{_CN_NUM})"
)
# 反过来："三千预算" / "三千左右"
_CN_BEFORE_BUDGET = re.compile(
    rf"(?P<cn>{_CN_NUM})\s*(?:元|块)?\s*(?:预算|左右|上下|以内|以下)"
)

_SCOPE_EXCLUDE = (
    "不含装备", "装备另算", "装备另计", "装备不算", "装备除外",
    "装备单独算", "装备单算", "装备自己买", "不含装备预算",
)
_SCOPE_INCLUDE = ("含装备", "包含装备", "装备也算", "算上装备", "预算含装备", "连装备")

_CAMP_WORDS = ("露营", "营地", "扎营", "帐篷过夜")
_HOTEL_WORDS = ("酒店", "民宿", "青旅", "宾馆", "客栈", "住店")
_KID_WORDS = ("孩子", "小孩", "儿童", "宝宝", "亲子", "带娃", "带孩", "小朋友")
_FLIGHT_WORDS = ("飞机", "航班", "机票", "飞过去")
_TRAIN_WORDS = ("高铁", "动车", "火车")
_DRIVE_WORDS = ("自驾", "开车", "租车", "自驾游")

# ---- 指定活动与"想参加活动"的意向 ----
# 分两档语气：已订是硬约束，想去只是偏好；两者都不能和"想参加一点活动"混为一谈。
_EVENT_BOOKED_CUES = (
    "已订好", "已订", "订好了", "已买票", "已经买票", "买票了", "买好票", "票已经买",
    "已预约", "已经预约", "约好了", "已经报名", "报了名",
)
# 只有这些才说明"用户想要活动"这个取向；单纯出现"活动"两个字不算
_EVENT_INTENT_CUES = (
    "想参加活动", "想参与活动", "喜欢参加活动", "想参加一点活动", "想参与一点活动",
    "多安排活动", "活动多一点", "多安排一点活动", "不要只看景点", "想有活动",
    "演出", "工作坊", "体验课", "音乐会", "逛市集",
)
# 只切句末，不切逗号：金额和日期常在逗号后面
_EVENT_STOP = re.compile(r"[。；！？;\n]")
# 长的放前面，先命中先匹配（"音乐会" 要先于 "音乐"）
_EVENT_KIND_WORDS = (
    "音乐会", "演唱会", "音乐节", "音乐剧", "话剧", "演出", "展览", "展会",
    "工作坊", "体验课", "课程", "市集", "沙龙", "讲座", "比赛", "首映",
    "city walk", "一日游", "半日游", "徒步", "骑行", "潜水", "滑雪", "温泉",
)
_EVENT_BOUNDARY = re.compile(r"[的去了在个场次看听参加上逛和与及、]")
MAX_EVENT_TITLE_PREFIX = 6
# 泛指词：抽到的名字落在这些上面说明用户没指名，不能当成一个日程
_EVENT_GENERIC = {"活动", "一点活动", "一些活动", "点活动", "些活动", "个活动", "节目"}
_EVENT_DAY = re.compile(r"第\s*(\d+)\s*天")
_EVENT_HHMM = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_EVENT_HOUR = re.compile(r"(\d{1,2})\s*[点时]")
_EVENT_PRICE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
_EVENT_DATE = re.compile(r"(?:(20\d{2})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
# "在上海音乐厅看XX" → 地点 + 动作 + 标题
_EVENT_VENUE = re.compile(r"(?:^|[了，,、\s])在\s*(.{2,14}?)\s*(?:看|听|参加|上|逛)\s*(.+)$")
_EVENT_LEAD = re.compile(r"^[了，,、\s的是]+")



@dataclass
class Slots:
    """一条消息里能抽到的全部槽位。``None`` 表示"用户没说"，不是 0。"""

    destination: str = ""
    days: Optional[int] = None
    people: Optional[int] = None
    budget: Optional[float] = None
    gear_budget: Optional[float] = None
    budget_scope: str = "all_in"
    start_date: Optional[str] = None
    lodging: str = ""
    transport: str = ""
    with_kids: bool = False
    fixed_events: List[Dict[str, Any]] = field(default_factory=list)
    wants_activity: bool = False
    sources: Dict[str, str] = field(default_factory=dict)

    def source(self, name: str) -> str:
        return self.sources.get(name, "")


# ---------------------------------------------------------------- 目的地


_TRIM_SUFFIXES = tuple(
    sorted(
        set(lexicon.DESTINATION_SUFFIXES)
        | set(lexicon.TRAVEL_SCENE)
        | set(lexicon.GEAR_TERMS)
        | {"什么", "啥", "吗", "呢", "的", "了", "带", "拿", "穿", "吃", "看", "玩"},
        key=len,
        reverse=True,
    )
)
_TIME_TAIL = re.compile(r"[\d零〇一二两三四五六七八九十百几]+\s*(?:天|日|晚|周|星期|礼拜)$")


def _trim_destination(candidate: str) -> str:
    text = (candidate or "").strip()
    changed = True
    while changed and text:
        changed = False
        trimmed = _TIME_TAIL.sub("", text)
        for suffix in _TRIM_SUFFIXES:
            if trimmed.endswith(suffix) and len(trimmed) > len(suffix):
                trimmed = trimmed[: -len(suffix)]
                break
        if trimmed != text:
            text = trimmed
            changed = True
    return text


def _valid_destination(candidate: str) -> bool:
    text = (candidate or "").strip()
    if len(text) < 2 or len(text) > 8:
        return False
    if text in lexicon.DESTINATION_STOPWORDS:
        return False
    if any(text.startswith(word) for word in lexicon.DESTINATION_STOPWORDS):
        return False
    # "去露营"抓到的是场景词，不是地名
    if text in lexicon.TRAVEL_SCENE or text in lexicon.TRAVEL_STRONG:
        return False
    if text in lexicon.DESTINATION_SUFFIXES:
        return False
    if re.fullmatch(r"\d+", text):
        return False
    return True


def parse_destination(text: str) -> str:
    """先按"去 X"抓，再按"X 两天"抓，最后查地名表。"""
    source = str(text or "")

    for match in _DEST_VERB.finditer(source):
        run = _CJK_RUN.match(source, match.end())
        if not run:
            continue
        raw = run.group(0)
        # 先看已知地名是不是这段的前缀："去成都吃火锅" -> 成都，
        # "去莫干山露营两天带什么" -> 莫干山
        known = next((place for place in _PLACES_BY_LENGTH if raw.startswith(place)), "")
        if known:
            return known
        candidate = _trim_destination(raw)
        if _valid_destination(candidate):
            return candidate

    for place in sorted(lexicon.PLACES, key=len, reverse=True):
        if place in source:
            return place

    # 地名表没覆盖时，用"X 两天"兜底；先剪掉"并规划""再去"这类前缀
    for match in _PLACE_BEFORE_DURATION.finditer(source):
        candidate = _trim_destination(match.group(1))
        candidate = _LEADING_PARTICLES.sub("", candidate)
        if _valid_destination(candidate):
            return candidate
    return ""


# ---------------------------------------------------------------- 人数


def parse_people(text: str) -> Optional[int]:
    source = str(text or "")

    match = _PEOPLE_BIG_SMALL.search(source)
    if match:
        big = cn_or_arabic(match.group("big"))
        small = cn_or_arabic(match.group("small"))
        if big is not None and small is not None:
            return int(big + small)

    match = _PEOPLE_FAMILY.search(source)
    if match:
        value = cn_or_arabic(match.group("n"))
        if value:
            return int(value)

    match = _PEOPLE_COUNT.search(source)
    if match:
        value = cn_or_arabic(match.group("n"))
        if value:
            return int(value)

    if any(word in source for word in _PEOPLE_PAIR_WORDS):
        if _PEOPLE_RELATION.search(source) and any(word in source for word in ("孩子", "娃", "女儿", "儿子")):
            return 3
        return 2
    if any(word in source for word in _PEOPLE_ALONE_WORDS):
        return 1
    if _PEOPLE_RELATION.search(source):
        return 2
    return None


def parse_people_detail(text: str):
    value = parse_people(text)
    if value:
        return value, "用户原话"
    return None, "未指定"


# ---------------------------------------------------------------- 天数


def parse_days(text: str) -> Optional[int]:
    source = str(text or "")

    match = _DAYS.search(source)
    if match:
        value = cn_or_arabic(match.group("n"))
        if value:
            return int(value)

    match = _NIGHTS.search(source)
    if match:
        value = cn_or_arabic(match.group("n"))
        if value:
            # "3 晚" ≈ 4 天（住 3 晚占 4 个白天）
            return int(value) + 1

    if "周末" in source:
        return 2
    if "小长假" in source:
        return 3

    if _HALF_MONTH.search(source):
        return 15
    match = _WEEKS.search(source)
    if match:
        token = match.group("n")
        if token == "半":
            return 4
        value = cn_or_arabic(token)
        if value:
            return max(1, int(round(value * 7)))
    match = _MONTHS.search(source)
    if match:
        value = cn_or_arabic(match.group("n"))
        if value:
            return int(value * 30)
    return None


def parse_days_detail(text: str):
    value = parse_days(text)
    return (value, "用户原话") if value else (None, "未指定")


# ---------------------------------------------------------------- 预算


_CUE_TABLE = (
    [(cue, "gear") for cue in _GEAR_CUES]
    + [(cue, "max") for cue in _MAX_CUES]
    + [(cue, "total") for cue in _TOTAL_CUES]
    + [(cue, "min") for cue in _MIN_CUES]
)


def _nearest_cue(before: str):
    """取离金额最近的那个提示词——"装备预算1000，总预算3000"里的 3000 该算总预算。"""
    best = ("", -1, "")
    for cue, kind in _CUE_TABLE:
        index = before.rfind(cue)
        if index < 0:
            continue
        end = index + len(cue)
        if (end, len(cue)) >= (best[1], len(best[0])):
            best = (cue, end, kind)
    return best  # (cue, end, kind)


def _kind_of(source: str, start: int, end: int) -> str:
    before = source[max(0, start - 14):start]
    after = source[end:end + 12]
    tail = before[-10:]

    cue, cue_end, kind = _nearest_cue(before)
    if cue:
        # 提示词后面又出现了商品词，说明这笔钱是那件东西的价，不是行程预算
        product_pos = max(
            [tail.rfind(term) for term in lexicon.PRODUCT_NOUNS if term in tail] or [-1]
        )
        cue_pos_in_tail = cue_end - (len(before) - len(tail))
        if product_pos > cue_pos_in_tail:
            return "item"
        return kind

    # 商品单价："800元以内的跑鞋" / "手机5000" —— 不是这次出行的预算
    if any(term in tail for term in lexicon.PRODUCT_NOUNS):
        return "item"
    if "的" in after and any(term in after for term in lexicon.PRODUCT_NOUNS):
        return "item"

    if any(cue in after for cue in _TOTAL_CUES):
        return "total"
    if any(cue in after for cue in _MAX_CUES):
        return "max"
    if any(cue in after for cue in _MIN_CUES):
        return "min"
    return "plain"


def _budget_candidates(text: str):
    source = str(text or "")
    candidates = []
    for amount in find_amounts(source):
        candidates.append((amount.value, amount.start, amount.end))
    # 不带单位的数字：只在明确预算词附近，或者后面紧跟"以内/左右"时才算金额
    for pattern in (_BARE_AMOUNT, _BARE_AFTER, _BARE_BEFORE_BUDGET):
        for match in pattern.finditer(source):
            start, end = match.start("num"), match.end("num")
            if any(start >= item[1] and end <= item[2] for item in candidates):
                continue
            candidates.append((float(match.group("num")), start, end))
    # 中文数字 + 预算词（"预算三千"）
    for pattern in (_BARE_CN, _CN_BEFORE_BUDGET):
        for match in pattern.finditer(source):
            start, end = match.start("cn"), match.end("cn")
            if any(start >= item[1] and end <= item[2] for item in candidates):
                continue
            value = cn_or_arabic(match.group("cn"))
            if value:
                candidates.append((float(value), start, end))
    return sorted(candidates)


def parse_budget_detail(text: str):
    """返回 (总预算, 判定依据)。

    先剔除装备预算和商品单价，再按"总预算 > 上限 > 下限 > 唯一金额"取。
    """
    source = str(text or "")
    buckets: Dict[str, list] = {"total": [], "max": [], "min": [], "plain": [], "gear": [], "item": []}
    for value, start, end in _budget_candidates(source):
        buckets[_kind_of(source, start, end)].append(value)

    for kind in ("total", "max", "min"):
        if buckets[kind]:
            return buckets[kind][-1], kind
    if len(buckets["plain"]) == 1:
        return buckets["plain"][0], "plain"
    return None, ""


def parse_budget(text: str) -> Optional[float]:
    return parse_budget_detail(text)[0]


def parse_gear_budget(text: str) -> Optional[float]:
    source = str(text or "")
    for value, start, end in _budget_candidates(source):
        if _kind_of(source, start, end) == "gear":
            return value
    match = re.search(rf"装备\s*(?:预算|费用|花费)?\s*(?:为|是|:|：)?\s*(?P<num>\d+(?:\.\d+)?)", source)
    if match:
        return float(match.group("num"))
    return None


def parse_budget_scope(text: str):
    source = str(text or "")
    if any(word in source for word in _SCOPE_EXCLUDE):
        return "trip_only", "用户原话"
    if any(word in source for word in _SCOPE_INCLUDE):
        return "all_in", "用户原话"
    return "all_in", "默认规则"


# ---------------------------------------------------------------- 日期


def _today(today=None) -> date:
    if today is None:
        return date.today()
    if isinstance(today, date):
        return today
    return date.fromisoformat(str(today)[:10])


def _shift_weekday(base: date, weekday: int, next_week: bool) -> date:
    if next_week:
        # "下周三"= 下周一 + 2；不是简单的"本周三 + 7"
        next_monday = base + timedelta(days=7 - base.weekday())
        return next_monday + timedelta(days=weekday)
    return base + timedelta(days=(weekday - base.weekday()) % 7)


def parse_start_date(text: str, today=None) -> Optional[str]:
    source = str(text or "")
    base = _today(today)

    match = _ISO_DATE.search(source)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()

    match = _CN_DATE.search(source)
    if match:
        year = int(match.group(1)) if match.group(1) else base.year
        candidate = date(year, int(match.group(2)), int(match.group(3)))
        if not match.group(1) and candidate < base:
            candidate = date(year + 1, int(match.group(2)), int(match.group(3)))
        return candidate.isoformat()

    for word, offset in _RELATIVE_DAYS.items():
        if word in source:
            return (base + timedelta(days=offset)).isoformat()

    for word, weekday in _WEEKDAY_WORDS.items():
        if word not in source:
            continue
        prefix = source[max(0, source.find(word) - 2):source.find(word)]
        return _shift_weekday(base, weekday, next_week=("下" in prefix)).isoformat()

    if "周末" in source:
        return _shift_weekday(base, 5, next_week=("下" in source)).isoformat()

    for word, (month, day) in _FIXED_HOLIDAYS.items():
        if word in source:
            candidate = date(base.year, month, day)
            if candidate < base:
                candidate = date(base.year + 1, month, day)
            return candidate.isoformat()

    match = _SLASH_DATE.search(source)
    if match:
        candidate = date(base.year, int(match.group(1)), int(match.group(2)))
        if candidate < base:
            candidate = date(base.year + 1, int(match.group(1)), int(match.group(2)))
        return candidate.isoformat()
    return None


# ---------------------------------------------------------------- 住宿 / 交通 / 同行


def parse_lodging(text: str) -> str:
    source = str(text or "")
    if any(word in source for word in _CAMP_WORDS):
        return "camp"
    if any(word in source for word in _HOTEL_WORDS):
        return "hotel"
    return ""


def parse_transport(text: str) -> str:
    source = str(text or "")
    if any(word in source for word in _FLIGHT_WORDS):
        return "flight"
    if any(word in source for word in _TRAIN_WORDS):
        return "train"
    if any(word in source for word in _DRIVE_WORDS):
        return "drive"
    return ""


def parse_with_kids(text: str) -> bool:
    source = str(text or "")
    if any(word in source for word in _KID_WORDS):
        return True
    return bool(_PEOPLE_BIG_SMALL.search(source))


# ---------------------------------------------------------------- 汇总


def parse_wants_activity(text: str) -> bool:
    """用户是否表达了"想参加活动"的取向（没有指名具体哪场）。

    注意这只表示取向，不产生日程；把"想参加一点活动"当成一条活动安排，
    等于凭空替用户做了决定。
    """
    source = str(text or "")
    return any(cue in source for cue in _EVENT_INTENT_CUES)


def _event_title(clause: str) -> str:
    """从短语里截出带活动类型词的名字（"好下午的陶艺工作坊" → "陶艺工作坊"）。"""
    for kind in _EVENT_KIND_WORDS:
        position = clause.find(kind)
        if position < 0:
            continue
        chunk = ""
        for char in reversed(clause[:position]):
            if _EVENT_BOUNDARY.match(char) or len(chunk) >= MAX_EVENT_TITLE_PREFIX:
                break
            chunk = char + chunk
        return f"{chunk}{kind}".strip()
    return ""


def parse_fixed_events(text: str) -> List[Dict[str, Any]]:
    """抽取用户点名的固定活动，**只认已订语气**。

    两道闸，缺一不可：

    1. 必须命中 ``_EVENT_BOOKED_CUES``（已订/买票/预约/报名）。"想去上海"是
       目的地不是活动，把它概括成日程是这类抽取最容易犯的错。
    2. 名字里必须出现活动类型词（音乐会/工作坊/展览……）。用户说"想参加一点
       活动"时抽不到类型词，正好落在闸外，不会被当成一条日程。

    结果是 ``booked``；"想去看看"这类只是取向，由 ``parse_wants_activity`` 负责。
    """
    source = str(text or "")
    events: List[Dict[str, Any]] = []
    seen = set()
    consumed: List[tuple] = []

    # 长语气词优先："已订好陶艺工作坊" 不能被 "已订" 抢走，否则标题会剩下"好陶艺工作坊"
    for cue in sorted(_EVENT_BOOKED_CUES, key=len, reverse=True):
        for match in re.finditer(re.escape(cue), source):
            if any(start <= match.start() < end for start, end in consumed):
                continue
            consumed.append((match.start(), match.end()))
            clause = _EVENT_STOP.split(source[match.end():], 1)[0].strip()
            clause = _EVENT_LEAD.sub("", clause)
            if not clause:
                continue

            venue = ""
            venue_match = _EVENT_VENUE.match(clause)
            if venue_match:
                venue, clause = venue_match.group(1).strip(), venue_match.group(2).strip()

            day = 0
            day_match = _EVENT_DAY.search(clause)
            if day_match:
                day = int(day_match.group(1))
                clause = _EVENT_DAY.sub(" ", clause)

            date_text = ""
            date_match = _EVENT_DATE.search(clause)
            if date_match:
                year, month, day_of_month = date_match.group(1), int(date_match.group(2)), int(date_match.group(3))
                date_text = f"{year}-{month:02d}-{day_of_month:02d}" if year else f"{month:02d}-{day_of_month:02d}"
                clause = _EVENT_DATE.sub(" ", clause)

            start = ""
            start_match = _EVENT_HHMM.search(clause)
            if start_match:
                start = f"{int(start_match.group(1)):02d}:{start_match.group(2)}"
            else:
                hour_match = _EVENT_HOUR.search(clause)
                if hour_match:
                    start = f"{int(hour_match.group(1)):02d}:00"

            cost = 0.0
            price_match = _EVENT_PRICE.search(clause)
            if price_match:
                cost = float(price_match.group(1))

            title = _event_title(clause)
            if not title or title in _EVENT_GENERIC or title in seen:
                continue
            seen.add(title)
            events.append({
                "title": title,
                "day": day,
                "date": date_text,
                "start": start,
                "venue": venue,
                "cost": cost,
                "status": "booked",
                "source": "用户原话",
            })
    return events


def extract_slots(text: str, today=None) -> Slots:
    source = str(text or "")
    budget, budget_kind = parse_budget_detail(source)
    gear_budget = parse_gear_budget(source)
    scope, scope_source = parse_budget_scope(source)
    if gear_budget is not None:
        scope, scope_source = "trip_only", "用户原话"

    destination = parse_destination(source)
    days, days_source = parse_days_detail(source)
    if days is None and "周末" in source:
        days, days_source = 2, "规则推导（周末按 2 天）"
    people, people_source = parse_people_detail(source)

    slots = Slots(
        destination=destination,
        days=days,
        people=people,
        budget=budget,
        gear_budget=gear_budget,
        budget_scope=scope,
        start_date=parse_start_date(source, today),
        lodging=parse_lodging(source),
        transport=parse_transport(source),
        with_kids=parse_with_kids(source),
        fixed_events=parse_fixed_events(source),
        wants_activity=parse_wants_activity(source),
    )
    slots.sources.update({
        "destination": "用户原话" if destination else "未指定",
        "days": days_source,
        "people": people_source,
        "budget": f"用户原话（{budget_kind}）" if budget is not None else "未指定",
        "budget_scope": scope_source,
        "start_date": "用户原话" if slots.start_date else "未指定",
        "lodging": "用户原话" if slots.lodging else "未指定",
        "transport": "用户原话" if slots.transport else "未指定",
        "with_kids": "用户原话" if slots.with_kids else "未指定",
    })
    if gear_budget is not None:
        slots.sources["gear_budget"] = "用户原话"
    return slots
