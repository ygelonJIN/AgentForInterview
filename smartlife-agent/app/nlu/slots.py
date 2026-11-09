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

# 目的地：动词后面跟 2~8 个汉字。
# 分强弱两档：`我/我们` 后面的"在"说的是"人在哪儿"（出发地），不是目的地。
# "我在广东，我要去上海" 里先扫强动词才抽得到上海，否则第一个命中的"在广东"直接胜出。
_DEST_VERB_STRONG = re.compile(
    r"(?:想去|准备去|打算去|坐高铁去|自驾去|前往|飞往|飞到|回到|去|到|飞|回)"
)
_DEST_VERB_WEAK = re.compile(r"(?:在)")
_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]{2,8}")

# 出发地：``我在广东`` / ``从广州出发``。必须和目的地分开抽——
# 合成一个槽位就是二选一，选错整趟行程的方向都反了。
_ORIGIN_IN = re.compile(
    r"(?:我|我们|本人|咱|俺)\s*(?:现在|目前|人)?\s*(?:在|住在|住|来自)\s*([\u4e00-\u9fa5]{2,6})"
)
_ORIGIN_FROM = re.compile(r"从\s*([\u4e00-\u9fa5]{2,6}?)\s*(?:出发|飞|坐|开车|过去|到|去|来)")
# "在上海出发去广东"：出发地不一定是"我"开头的句子
_ORIGIN_DEPART = re.compile(r"(?:在|从)\s*([\u4e00-\u9fa5]{2,6}?)\s*出发")
# "上海到广州"：没有"从"也要认出起点
_ORIGIN_TO = re.compile(r"(?:^|[，。；;、\s])\s*([\u4e00-\u9fa5]{2,6}?)\s*(?:到|至)\s*[\u4e00-\u9fa5]{2,6}")
_ORIGIN_SUBJECTS = ("我", "我们", "本人", "咱", "俺", "他", "她", "住", "住在")
_LEADING_PARTICLES = re.compile(r"^(?:并|和|再|然后|顺便|规划|安排|去|到|在|回)+")

# 否定：只看紧挨着的前几个字，且排除"特别/分别/区别"这类词里的"别"
_NEGATION_WORDS = ("不", "没", "别", "免", "无需")
_NEGATION_FALSE = ("特别", "分别", "区别", "个别")


def _negated_before(source: str, position: int, window: int = 3) -> bool:
    head = source[max(0, position - window):position]
    if any(word in head for word in _NEGATION_FALSE):
        return False
    return any(word in head for word in _NEGATION_WORDS)


# 景区/地标不是独立城市：出现在同一句里要归并到它的城市
_SUB_PLACES = {
    "鼓浪屿": "厦门", "曾厝垵": "厦门", "西湖": "杭州", "外滩": "上海",
    "迪士尼": "上海", "故宫": "北京", "兵马俑": "西安", "亚龙湾": "三亚",
    "宽窄巷子": "成都", "夫子庙": "南京", "回民街": "西安", "洱海": "大理",
}
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
# "广州、深圳、珠海各玩两天"：这是每城天数，整趟要乘城市数
_PER_CITY_DAYS = re.compile(rf"各\s*(?:玩|待|住|安排)?\s*(?P<n>{_NUM})\s*(?:天|日)")
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
# 农历节日没有固定公历日期：只列已知年份，查不到就不猜
_LUNAR_HOLIDAYS = {
    "春节": {2026: (2, 17), 2027: (2, 6), 2028: (1, 26)},
    "过年": {2026: (2, 17), 2027: (2, 6), 2028: (1, 26)},
    "清明": {2026: (4, 5), 2027: (4, 5), 2028: (4, 4)},
    "端午": {2026: (6, 19), 2027: (6, 9), 2028: (5, 28)},
    "中秋": {2026: (9, 25), 2027: (9, 15), 2028: (10, 3)},
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
    "不需要装备", "不用装备", "不带装备", "不买装备", "装备不买",
)
_SCOPE_INCLUDE = ("含装备", "包含装备", "装备也算", "算上装备", "预算含装备", "连装备")

# "酒店一晚不超400"：单价口径，不是住几晚，也不是总预算
_LODGING_RATE = re.compile(
    r"(?:(?:酒店|住宿|房费|民宿|住店)[^，。；;！？\n]{0,10}?)?"
    r"(?:一晚|每晚|一晚上|每夜)[^0-9，。；;！？\n]{0,8}(?P<num>\d{2,5}(?:\.\d+)?)"
)

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
# "有一天去找朋友" 这类约见：人和地点是明确的，哪天不明确
_VISIT_PEOPLE = ("朋友", "同学", "亲戚", "家人", "发小", "闺蜜", "老乡")
_VISIT_VERBS = ("探望", "拜访", "约见", "看看", "去找", "去找个", "见", "找", "看")
_VISIT_CUE = re.compile(
    rf"(?P<verb>{'|'.join(_VISIT_VERBS)})\s*(?:一个|一位|个|位|下)?\s*(?:我|你)?\s*"
    rf"(?:在\s*[\u4e00-\u9fa5]{{2,6}}\s*的)?\s*(?P<who>{'|'.join(_VISIT_PEOPLE)})"
)
_VISIT_PLACE = re.compile(
    rf"(?:在|去|到)\s*([\u4e00-\u9fa5]{{2,6}}?)\s*的\s*(?:{'|'.join(_VISIT_PEOPLE)})"
)
# "去浦东找朋友"：地点夹在"去"和"找"之间
_VISIT_PLACE_VERB = re.compile(
    rf"(?:去|到)\s*([\u4e00-\u9fa5]{{2,6}}?)\s*(?:{'|'.join(_VISIT_VERBS)})"
)
# "在上海音乐厅看XX" → 地点 + 动作 + 标题
_EVENT_VENUE = re.compile(r"(?:^|[了，,、\s])在\s*(.{2,14}?)\s*(?:看|听|参加|上|逛)\s*(.+)$")
_EVENT_LEAD = re.compile(r"^[了，,、\s的是]+")



@dataclass
class Slots:
    """一条消息里能抽到的全部槽位。``None`` 表示"用户没说"，不是 0。"""

    destination: str = ""
    origin: str = ""
    cities: List[str] = field(default_factory=list)
    days: Optional[int] = None
    people: Optional[int] = None
    budget: Optional[float] = None
    lodging_budget: Optional[float] = None
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
    # 夹着虚词或动词的，是半截句子不是地名（"找一个我在浦东"）。地名里没有这几个字。
    if any(char in text for char in "我的了在找去"):
        return False
    # "去露营"抓到的是场景词，不是地名
    if text in lexicon.TRAVEL_SCENE or text in lexicon.TRAVEL_STRONG:
        return False
    if text in lexicon.DESTINATION_SUFFIXES:
        return False
    if re.fullmatch(r"\d+", text):
        return False
    return True


def _follows_subject(source: str, position: int) -> bool:
    """"我/我们"后面紧跟的"在"是"人在哪"，不是"要去哪"。"""
    return source[:position].rstrip().endswith(_ORIGIN_SUBJECTS)


def _verb_destination(source: str, pattern, skip_origin: bool) -> str:
    for match in pattern.finditer(source):
        if _negated_before(source, match.start()):
            continue
        if skip_origin and _follows_subject(source, match.start()):
            continue
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
    return ""


def parse_destination(text: str) -> str:
    """先按"去 X"抓，再按"X 两天"抓，最后查地名表。"""
    source = str(text or "")

    # 强动词（去/到/回/飞）优先于"在"：一句话里同时出现起点和终点时，
    # "去上海"必须赢过"在广东"。
    found = _verb_destination(source, _DEST_VERB_STRONG, skip_origin=False)
    if found:
        return found
    found = _verb_destination(source, _DEST_VERB_WEAK, skip_origin=True)
    if found:
        return found

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


def parse_origin(text: str) -> str:
    """出发地："我在广东" 里的广东。

    目的地和出发地同句出现是常态（"我在广东，我要去上海"）。只抽目的地，
    起点就没了，行程里的交通和时长也只能靠猜；只抽到一个槽位更糟——会把
    起点当终点。
    """
    source = str(text or "")
    for pattern in (_ORIGIN_IN, _ORIGIN_FROM, _ORIGIN_DEPART, _ORIGIN_TO):
        for match in pattern.finditer(source):
            candidate = _trim_destination(match.group(1))
            if _valid_destination(candidate):
                return candidate
    return ""


def parse_cities(text: str, origin: str = "") -> List[str]:
    """按出现顺序抽所有城市，用于"去广东的很多城市"这类多城需求。

    只认地名表里的城市；省份不算城市。出发地要排掉，否则"从上海去广州"
    会把上海也算进行程城市。
    """
    source = str(text or "")
    hits = []
    for place in lexicon.PLACES:
        start = source.find(place)
        if start < 0:
            continue
        hits.append((start, -len(place), place))
    ordered: List[str] = []
    for start, _neg_len, place in sorted(hits):
        if place == origin or place in ordered:
            continue
        if _negated_before(source, start, window=4):
            continue
        ordered.append(place)
    # 鼓浪屿归厦门、西湖归杭州：景点不算多城市
    return [
        place
        for place in ordered
        if not (place in _SUB_PLACES and _SUB_PLACES[place] in ordered)
    ]


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

    extras = _relation_extras(source)
    if extras:
        return 1 + extras
    if any(word in source for word in _PEOPLE_PAIR_WORDS):
        if _PEOPLE_RELATION.search(source) and any(word in source for word in ("孩子", "娃", "女儿", "儿子")):
            return 3
        return 2
    if any(word in source for word in _PEOPLE_ALONE_WORDS):
        return 1
    if _PEOPLE_RELATION.search(source):
        return 2
    return None


def _relation_extras(source: str) -> int:
    """"带爸妈/带娃/和朋友"里跟着的人数；自己算 1 个，由调用方加上。"""
    total = 0
    if re.search(r"带(?:上|着)?(?:爸妈|父母|爹妈|双亲)", source):
        total += 2
    elif re.search(r"带(?:上|着)?(?:老人|长辈|爷爷|奶奶|外公|外婆)", source):
        total += 1
    if re.search(r"带(?:上|着)?(?:娃|孩子|小孩|儿童|宝宝|小朋友)", source):
        total += 1
    if re.search(r"(?:和|跟|与|带上)(?:一个|一位|个)?(?:朋友|同事|同学|闺蜜|发小|伴侣|对象)", source):
        total += 1
    return total


def parse_people_detail(text: str):
    value = parse_people(text)
    if value:
        return value, "用户原话"
    return None, "未指定"


# ---------------------------------------------------------------- 天数


_RATE_AFTER = re.compile(
    r"^\s*(?:不超|不超过|以内|以下|上限|最多|不到|低于|\d|"
    r"[零〇一二两三四五六七八九十百千]+\s*(?:元|块))"
)


def _nights_is_price(source: str, match) -> bool:
    """判断这个"N 晚"是住宿时长还是房价口径。"""
    before = source[max(0, match.start() - 4):match.start()]
    if any(word in before for word in ("每", "酒店", "房价", "住宿", "房")):
        return True
    return bool(_RATE_AFTER.match(source[match.end():]))


def parse_days(text: str) -> Optional[int]:
    source = str(text or "")

    match = _DAYS.search(source)
    if match:
        value = cn_or_arabic(match.group("n"))
        if value:
            return int(value)

    for match in _NIGHTS.finditer(source):
        # "酒店一晚不超400" 里的"一晚"是房价单位，不是住几晚
        if _nights_is_price(source, match):
            continue
        value = cn_or_arabic(match.group("n"))
        if value:
            # "3 晚" ≈ 4 天（住 3 晚占 4 个白天）
            return int(value) + 1

    if any(word in source for word in ("当天来回", "当天往返", "一日游", "一天往返", "当日往返")):
        return 1
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


_PER_PERSON_CUES = ("人均", "每人", "一人")


def parse_per_person_budget(text: str) -> Optional[float]:
    """人均预算：只认"人均/每人 + 金额"，不和人头数相乘（换算在外层做）。"""
    source = str(text or "")
    for cue in _PER_PERSON_CUES:
        match = re.search(rf"{cue}[^0-9]{{0,4}}(\d+(?:\.\d+)?)", source)
        if match:
            return float(match.group(1))
        match = re.search(rf"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:左右)?{cue}", source)
        if match:
            return float(match.group(1))
    return None


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

    if "下个月" in source or "下月" in source:
        year = base.year + (1 if base.month == 12 else 0)
        month = 1 if base.month == 12 else base.month + 1
        return date(year, month, 1).isoformat()

    if "月底" in source:
        year = base.year + (1 if base.month == 12 else 0)
        month = 1 if base.month == 12 else base.month + 1
        return (date(year, month, 1) - timedelta(days=1)).isoformat()

    for word, table in _LUNAR_HOLIDAYS.items():
        if word not in source:
            continue
        for year in (base.year, base.year + 1):
            if year in table:
                candidate = date(year, *table[year])
                if candidate >= base:
                    return candidate.isoformat()
        return None

    for word, (month, day) in _FIXED_HOLIDAYS.items():
        # "十一月"不是"十一"：节日词后面不许紧跟"月"
        if re.search(re.escape(word) + r"(?!月)", source):
            candidate = date(base.year, month, day)
            if candidate < base:
                candidate = date(base.year + 1, month, day)
            return candidate.isoformat()

    # "三月去婺源"：只有月份时取该月 1 号，已过就顺延到明年
    month_match = re.search(
        r"(?<!\d)([一二三四五六七八九十]{1,3}|[1-9]|1[0-2])\s*月(?!\s*[\d一二三四五六七八九十])",
        source,
    )
    if month_match:
        month = cn_or_arabic(month_match.group(1))
        if month:
            candidate = date(base.year, int(month), 1)
            if candidate < base:
                candidate = date(base.year + 1, int(month), 1)
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
    for word in _CAMP_WORDS:
        position = source.find(word)
        if position >= 0 and not _negated_before(source, position):
            return "camp"
    for word in _HOTEL_WORDS:
        position = source.find(word)
        if position >= 0 and not _negated_before(source, position):
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


def parse_visits(text: str) -> List[Dict[str, Any]]:
    """抽"有一天去找朋友""第 2 天去浦东见同学"这类约见。

    和 ``parse_fixed_events`` 分开抽，是因为硬度不同：已订票的活动钉死在
    某一天，约朋友只保证"这趟得有半天"，哪天可以调。当成 ``booked`` 会假装
    用户买过票（还会挤掉别的安排），当成 ``planned`` 又会被行程挤掉，
    所以给它单独一档 ``appointment``：地点和事由固定，日期由排程决定。
    """
    source = str(text or "")
    match = _VISIT_CUE.search(source)
    if not match:
        return []

    venue = ""
    place = _VISIT_PLACE.search(source) or _VISIT_PLACE_VERB.search(source)
    if place:
        venue = place.group(1)

    day = 0
    day_match = _EVENT_DAY.search(source)
    if day_match:
        day = int(day_match.group(1))

    verb = match.group("verb")
    if verb.startswith("去"):
        verb = verb[1:] or "找"
    return [{
        "title": f"{verb}{match.group('who')}",
        "day": day,
        "date": "",
        "start": "",
        "venue": venue,
        "cost": 0.0,
        "duration_min": 180,
        "status": "appointment",
        "source": "用户原话",
    }]


def parse_lodging_budget(text: str) -> Optional[float]:
    """每晚住宿预算：只认"一晚/每晚 + 金额"的写法，取值范围 50~3000。"""
    match = _LODGING_RATE.search(str(text or ""))
    if not match:
        return None
    try:
        value = float(match.group("num"))
    except ValueError:
        return None
    return value if 50 <= value <= 3000 else None


def extract_slots(text: str, today=None) -> Slots:
    source = str(text or "")
    budget, budget_kind = parse_budget_detail(source)
    per_person = parse_per_person_budget(source)
    lodging_budget = parse_lodging_budget(source)
    gear_budget = parse_gear_budget(source)
    scope, scope_source = parse_budget_scope(source)
    if gear_budget is not None:
        scope, scope_source = "trip_only", "用户原话"

    destination = parse_destination(source)
    origin = parse_origin(source)
    cities = parse_cities(source, origin=origin)
    days, days_source = parse_days_detail(source)
    each = _PER_CITY_DAYS.search(source)
    if each and len(cities) >= 2:
        per_city = cn_or_arabic(each.group("n"))
        if per_city:
            days, days_source = int(per_city) * len(cities), "规则推导（各城天数 × 城市数）"
    if days is None and "周末" in source:
        days, days_source = 2, "规则推导（周末按 2 天）"
    people, people_source = parse_people_detail(source)
    if budget is None and per_person is not None:
        budget = per_person * (people or 1)
        budget_kind = "人均×人数" if people else "人均（按 1 人计）"
    # "预算3000到5000"：区间按上限算，并在来源里写明
    range_match = re.search(r"预算[^\d]{0,4}(\d+)[^\d]{0,3}(?:到|至|~|-)[^\d]{0,3}(\d+)", source)
    if range_match:
        budget = float(range_match.group(2))
        budget_kind = f"区间上限（{range_match.group(1)}~{range_match.group(2)}）"

    slots = Slots(
        destination=destination,
        origin=origin,
        cities=cities,
        days=days,
        people=people,
        budget=budget,
        lodging_budget=lodging_budget,
        gear_budget=gear_budget,
        budget_scope=scope,
        start_date=parse_start_date(source, today),
        lodging=parse_lodging(source),
        transport=parse_transport(source),
        with_kids=parse_with_kids(source),
        fixed_events=parse_fixed_events(source) + parse_visits(source),
        wants_activity=parse_wants_activity(source),
    )
    slots.sources.update({
        "destination": "用户原话" if destination else "未指定",
        "origin": "用户原话" if origin else "未指定",
        "cities": "用户原话" if len(cities) >= 2 else "未指定",
        "days": days_source,
        "people": people_source,
        "budget": f"用户原话（{budget_kind}）" if budget is not None else "未指定",
        "lodging_budget": "用户原话" if lodging_budget is not None else "未指定",
        "budget_scope": scope_source,
        "start_date": "用户原话" if slots.start_date else "未指定",
        "lodging": "用户原话" if slots.lodging else "未指定",
        "transport": "用户原话" if slots.transport else "未指定",
        "with_kids": "用户原话" if slots.with_kids else "未指定",
    })
    if gear_budget is not None:
        slots.sources["gear_budget"] = "用户原话"
    return slots
