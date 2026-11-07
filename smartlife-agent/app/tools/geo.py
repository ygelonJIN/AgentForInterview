"""全球地理编码：把用户嘴里的中文地名解析成可靠坐标。

为什么要这一层
--------------
用户会随口问世界任何城市。原来的 ``providers._geocode`` 直接拿中文原词打
Open-Meteo geocoding，实测结果不可用：

- ``东京`` → 只命中浙江一个 ``PPL`` 村（日本东京的 GeoNames 别名是繁体「東京」）；
- ``纽约`` → 完全 MISS；
- Nominatim 兜底分支在本机 100% 连接超时，属于"写在那里但永远跑不到"的死路。

所以这里做三件事，优先级从高到低：

1. **别名表**：高频目的地先映射成规范英文名，再查坐标。这是唯一稳定的路径。
2. **择优排序**：多候选取 ``feature_code``（首都 > 省会 > 普通城市）+ 人口，
   不再盲取第一条。
3. **保守拒答**：表里明确知道该城市属于哪个国家时，只接受国家匹配的候选；
   匹配不上就抛错，由上层披露"定位不到"，绝不把江苏的村当成东京。

宁可说"定位不到"，也不给错坐标 —— 这条和 `docs/关键决策总表.md` 里
"规格缺失一律 unknown""不虚报满足"是同一个口径。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

# feature_code 优先级：数字越大越像"用户想去的那个地方"
# 权重刻意压得比人口低：地名冲突时"人口多的那个"才是用户想去的地方。
# 实测教训：PPLA2 权重给太高的版本里，"纽约"命中了内布拉斯加人口 7 千的 York。
_FEATURE_WEIGHT = {
    "PPLC": 30,   # 首都
    "ADM1": 25,   # 省级
    "PPLA": 20,   # 一级行政区首府
    "PPLA2": 15,
    "PPLA3": 10,
    "PPLA4": 6,
    "PPL": 5,     # 普通居民点
    "ADM2": 12,
    "ISL": 8,     # 岛屿（圣托里尼这类）
    "RSTN": 2,
}

# 中文（含常见别称/简繁混写）→ (规范中文名, 英文查询名, 国家码)
# 国家码用于"只接受同国候选"的校验；留空表示不校验。
CITY_ALIASES: Dict[str, Tuple[str, str, str]] = {
    # —— 亚洲 ——
    "新加坡": ("新加坡", "Singapore", "SG"),
    "狮城": ("新加坡", "Singapore", "SG"),
    "东京": ("东京", "Tokyo", "JP"),
    "東京": ("东京", "Tokyo", "JP"),
    "大阪": ("大阪", "Osaka", "JP"),
    "京都": ("京都", "Kyoto", "JP"),
    "札幌": ("札幌", "Sapporo", "JP"),
    "北海道": ("札幌", "Sapporo", "JP"),
    "冲绳": ("那霸", "Naha", "JP"),
    "那霸": ("那霸", "Naha", "JP"),
    "福冈": ("福冈", "Fukuoka", "JP"),
    "名古屋": ("名古屋", "Nagoya", "JP"),
    "首尔": ("首尔", "Seoul", "KR"),
    "汉城": ("首尔", "Seoul", "KR"),
    "釜山": ("釜山", "Busan", "KR"),
    "济州": ("济州", "Jeju", "KR"),
    "济州岛": ("济州", "Jeju", "KR"),
    "曼谷": ("曼谷", "Bangkok", "TH"),
    "清迈": ("清迈", "Chiang Mai", "TH"),
    "普吉": ("普吉", "Phuket", "TH"),
    "普吉岛": ("普吉", "Phuket", "TH"),
    "芭提雅": ("芭提雅", "Pattaya", "TH"),
    "吉隆坡": ("吉隆坡", "Kuala Lumpur", "MY"),
    "槟城": ("槟城", "George Town", "MY"),
    "沙巴": ("亚庇", "Kota Kinabalu", "MY"),
    "亚庇": ("亚庇", "Kota Kinabalu", "MY"),
    "雅加达": ("雅加达", "Jakarta", "ID"),
    "巴厘岛": ("登巴萨", "Denpasar", "ID"),
    "登巴萨": ("登巴萨", "Denpasar", "ID"),
    "峇里岛": ("登巴萨", "Denpasar", "ID"),
    "河内": ("河内", "Hanoi", "VN"),
    "胡志明": ("胡志明市", "Ho Chi Minh City", "VN"),
    "胡志明市": ("胡志明市", "Ho Chi Minh City", "VN"),
    "岘港": ("岘港", "Da Nang", "VN"),
    "马尼拉": ("马尼拉", "Manila", "PH"),
    "长滩岛": ("长滩岛", "Boracay", "PH"),
    "宿务": ("宿务", "Cebu City", "PH"),
    "金边": ("金边", "Phnom Penh", "KH"),
    "暹粒": ("暹粒", "Siem Reap", "KH"),
    "万象": ("万象", "Vientiane", "LA"),
    "仰光": ("仰光", "Yangon", "MM"),
    "加德满都": ("加德满都", "Kathmandu", "NP"),
    "科伦坡": ("科伦坡", "Colombo", "LK"),
    "马尔代夫": ("马累", "Male", "MV"),
    "马累": ("马累", "Male", "MV"),
    "新德里": ("新德里", "New Delhi", "IN"),
    "德里": ("德里", "Delhi", "IN"),
    "孟买": ("孟买", "Mumbai", "IN"),
    "班加罗尔": ("班加罗尔", "Bengaluru", "IN"),
    "迪拜": ("迪拜", "Dubai", "AE"),
    "阿布扎比": ("阿布扎比", "Abu Dhabi", "AE"),
    "多哈": ("多哈", "Doha", "QA"),
    "利雅得": ("利雅得", "Riyadh", "SA"),
    "特拉维夫": ("特拉维夫", "Tel Aviv", "IL"),
    "耶路撒冷": ("耶路撒冷", "Jerusalem", "IL"),
    "伊斯坦布尔": ("伊斯坦布尔", "Istanbul", "TR"),
    "安卡拉": ("安卡拉", "Ankara", "TR"),
    "德黑兰": ("德黑兰", "Tehran", "IR"),
    "塔什干": ("塔什干", "Tashkent", "UZ"),
    "阿拉木图": ("阿拉木图", "Almaty", "KZ"),
    "乌兰巴托": ("乌兰巴托", "Ulaanbaatar", "MN"),
    # —— 欧洲 ——
    "巴黎": ("巴黎", "Paris", "FR"),
    "尼斯": ("尼斯", "Nice", "FR"),
    "马赛": ("马赛", "Marseille", "FR"),
    "里昂": ("里昂", "Lyon", "FR"),
    "伦敦": ("伦敦", "London", "GB"),
    "爱丁堡": ("爱丁堡", "Edinburgh", "GB"),
    "曼彻斯特": ("曼彻斯特", "Manchester", "GB"),
    "都柏林": ("都柏林", "Dublin", "IE"),
    "阿姆斯特丹": ("阿姆斯特丹", "Amsterdam", "NL"),
    "鹿特丹": ("鹿特丹", "Rotterdam", "NL"),
    "布鲁塞尔": ("布鲁塞尔", "Brussels", "BE"),
    "柏林": ("柏林", "Berlin", "DE"),
    "慕尼黑": ("慕尼黑", "Munich", "DE"),
    "法兰克福": ("法兰克福", "Frankfurt", "DE"),
    "汉堡": ("汉堡", "Hamburg", "DE"),
    "科隆": ("科隆", "Cologne", "DE"),
    "苏黎世": ("苏黎世", "Zurich", "CH"),
    "日内瓦": ("日内瓦", "Geneva", "CH"),
    "因特拉肯": ("因特拉肯", "Interlaken", "CH"),
    "琉森": ("卢塞恩", "Lucerne", "CH"),
    "卢塞恩": ("卢塞恩", "Lucerne", "CH"),
    "维也纳": ("维也纳", "Vienna", "AT"),
    "萨尔茨堡": ("萨尔茨堡", "Salzburg", "AT"),
    "布拉格": ("布拉格", "Prague", "CZ"),
    "布达佩斯": ("布达佩斯", "Budapest", "HU"),
    "华沙": ("华沙", "Warsaw", "PL"),
    "克拉科夫": ("克拉科夫", "Krakow", "PL"),
    "罗马": ("罗马", "Rome", "IT"),
    "米兰": ("米兰", "Milan", "IT"),
    "威尼斯": ("威尼斯", "Venice", "IT"),
    "佛罗伦萨": ("佛罗伦萨", "Florence", "IT"),
    "那不勒斯": ("那不勒斯", "Naples", "IT"),
    "博洛尼亚": ("博洛尼亚", "Bologna", "IT"),
    "马德里": ("马德里", "Madrid", "ES"),
    "巴塞罗那": ("巴塞罗那", "Barcelona", "ES"),
    "塞维利亚": ("塞维利亚", "Seville", "ES"),
    "瓦伦西亚": ("瓦伦西亚", "Valencia", "ES"),
    "格拉纳达": ("格拉纳达", "Granada", "ES"),
    "里斯本": ("里斯本", "Lisbon", "PT"),
    "波尔图": ("波尔图", "Porto", "PT"),
    "雅典": ("雅典", "Athens", "GR"),
    "圣托里尼": ("圣托里尼", "Santorini", "GR"),
    "米克诺斯": ("米克诺斯", "Mykonos", "GR"),
    "札金索斯": ("扎金索斯", "Zakynthos", "GR"),
    "赫尔辛基": ("赫尔辛基", "Helsinki", "FI"),
    "斯德哥尔摩": ("斯德哥尔摩", "Stockholm", "SE"),
    "哥本哈根": ("哥本哈根", "Copenhagen", "DK"),
    "奥斯陆": ("奥斯陆", "Oslo", "NO"),
    "卑尔根": ("卑尔根", "Bergen", "NO"),
    "冰岛": ("雷克雅未克", "Reykjavik", "IS"),
    "雷克雅未克": ("雷克雅未克", "Reykjavik", "IS"),
    "塔林": ("塔林", "Tallinn", "EE"),
    "里加": ("里加", "Riga", "LV"),
    "维尔纽斯": ("维尔纽斯", "Vilnius", "LT"),
    "莫斯科": ("莫斯科", "Moscow", "RU"),
    "圣彼得堡": ("圣彼得堡", "Saint Petersburg", "RU"),
    "基辅": ("基辅", "Kyiv", "UA"),
    "贝尔格莱德": ("贝尔格莱德", "Belgrade", "RS"),
    "索菲亚": ("索菲亚", "Sofia", "BG"),
    "布加勒斯特": ("布加勒斯特", "Bucharest", "RO"),
    "卢布尔雅那": ("卢布尔雅那", "Ljubljana", "SI"),
    "萨格勒布": ("萨格勒布", "Zagreb", "HR"),
    "杜布罗夫尼克": ("杜布罗夫尼克", "Dubrovnik", "HR"),
    "斯普利特": ("斯普利特", "Split", "HR"),
    # —— 北美 ——
    "纽约": ("纽约", "New York", "US"),
    "紐約": ("纽约", "New York", "US"),
    "洛杉矶": ("洛杉矶", "Los Angeles", "US"),
    "旧金山": ("旧金山", "San Francisco", "US"),
    "三藩市": ("旧金山", "San Francisco", "US"),
    "西雅图": ("西雅图", "Seattle", "US"),
    "芝加哥": ("芝加哥", "Chicago", "US"),
    "波士顿": ("波士顿", "Boston", "US"),
    "华盛顿": ("华盛顿", "Washington", "US"),
    "迈阿密": ("迈阿密", "Miami", "US"),
    "奥兰多": ("奥兰多", "Orlando", "US"),
    "拉斯维加斯": ("拉斯维加斯", "Las Vegas", "US"),
    "夏威夷": ("檀香山", "Honolulu", "US"),
    "檀香山": ("檀香山", "Honolulu", "US"),
    "安克雷奇": ("安克雷奇", "Anchorage", "US"),
    "多伦多": ("多伦多", "Toronto", "CA"),
    "温哥华": ("温哥华", "Vancouver", "CA"),
    "蒙特利尔": ("蒙特利尔", "Montreal", "CA"),
    "班夫": ("班夫", "Banff", "CA"),
    "墨西哥城": ("墨西哥城", "Mexico City", "MX"),
    "坎昆": ("坎昆", "Cancun", "MX"),
    # —— 中南美 ——
    "里约热内卢": ("里约热内卢", "Rio de Janeiro", "BR"),
    "圣保罗": ("圣保罗", "Sao Paulo", "BR"),
    "布宜诺斯艾利斯": ("布宜诺斯艾利斯", "Buenos Aires", "AR"),
    "圣地亚哥": ("圣地亚哥", "Santiago", "CL"),
    "利马": ("利马", "Lima", "PE"),
    "库斯科": ("库斯科", "Cusco", "PE"),
    "波哥大": ("波哥大", "Bogota", "CO"),
    "哈瓦那": ("哈瓦那", "Havana", "CU"),
    # —— 大洋洲 ——
    "悉尼": ("悉尼", "Sydney", "AU"),
    "墨尔本": ("墨尔本", "Melbourne", "AU"),
    "布里斯班": ("布里斯班", "Brisbane", "AU"),
    "珀斯": ("珀斯", "Perth", "AU"),
    "凯恩斯": ("凯恩斯", "Cairns", "AU"),
    "黄金海岸": ("黄金海岸", "Gold Coast", "AU"),
    "奥克兰": ("奥克兰", "Auckland", "NZ"),
    "皇后镇": ("皇后镇", "Queenstown", "NZ"),
    "惠灵顿": ("惠灵顿", "Wellington", "NZ"),
    "斐济": ("苏瓦", "Suva", "FJ"),
    # —— 非洲 ——
    "开罗": ("开罗", "Cairo", "EG"),
    "开普敦": ("开普敦", "Cape Town", "ZA"),
    "约翰内斯堡": ("约翰内斯堡", "Johannesburg", "ZA"),
    "内罗毕": ("内罗毕", "Nairobi", "KE"),
    "卡萨布兰卡": ("卡萨布兰卡", "Casablanca", "MA"),
    "马拉喀什": ("马拉喀什", "Marrakesh", "MA"),
    "突尼斯": ("突尼斯", "Tunis", "TN"),
    "亚的斯亚贝巴": ("亚的斯亚贝巴", "Addis Ababa", "ET"),
    "拉各斯": ("拉各斯", "Lagos", "NG"),
    "毛里求斯": ("路易港", "Port Louis", "MU"),
    "塞舌尔": ("维多利亚", "Victoria", "SC"),
    # —— 中国（保持原有能力，仍走本地资料） ——
    "杭州": ("杭州", "Hangzhou", "CN"),
    "北京": ("北京", "Beijing", "CN"),
    "上海": ("上海", "Shanghai", "CN"),
    "广州": ("广州", "Guangzhou", "CN"),
    "深圳": ("深圳", "Shenzhen", "CN"),
    "成都": ("成都", "Chengdu", "CN"),
    "重庆": ("重庆", "Chongqing", "CN"),
    "西安": ("西安", "Xi'an", "CN"),
    "南京": ("南京", "Nanjing", "CN"),
    "苏州": ("苏州", "Suzhou", "CN"),
    "厦门": ("厦门", "Xiamen", "CN"),
    "青岛": ("青岛", "Qingdao", "CN"),
    "大连": ("大连", "Dalian", "CN"),
    "三亚": ("三亚", "Sanya", "CN"),
    "丽江": ("丽江", "Lijiang", "CN"),
    "大理": ("大理", "Dali", "CN"),
    "昆明": ("昆明", "Kunming", "CN"),
    "桂林": ("桂林", "Guilin", "CN"),
    "张家界": ("张家界", "Zhangjiajie", "CN"),
    "哈尔滨": ("哈尔滨", "Harbin", "CN"),
    "长沙": ("长沙", "Changsha", "CN"),
    "武汉": ("武汉", "Wuhan", "CN"),
    "天津": ("天津", "Tianjin", "CN"),
    "香港": ("香港", "Hong Kong", "HK"),
    "澳门": ("澳门", "Macau", "MO"),
    "台北": ("台北", "Taipei", "TW"),
    "高雄": ("高雄", "Kaohsiung", "TW"),
    "台中": ("台中", "Taichung", "TW"),
    "花莲": ("花莲", "Hualien", "TW"),
    "拉萨": ("拉萨", "Lhasa", "CN"),
    "乌鲁木齐": ("乌鲁木齐", "Urumqi", "CN"),
    "敦煌": ("敦煌", "Dunhuang", "CN"),
    "西宁": ("西宁", "Xining", "CN"),
    "兰州": ("兰州", "Lanzhou", "CN"),
    "贵阳": ("贵阳", "Guiyang", "CN"),
    "南宁": ("南宁", "Nanning", "CN"),
    "海口": ("海口", "Haikou", "CN"),
    "福州": ("福州", "Fuzhou", "CN"),
    "宁波": ("宁波", "Ningbo", "CN"),
    "无锡": ("无锡", "Wuxi", "CN"),
    "常州": ("常州", "Changzhou", "CN"),
    "温州": ("温州", "Wenzhou", "CN"),
    "绍兴": ("绍兴", "Shaoxing", "CN"),
    "嘉兴": ("嘉兴", "Jiaxing", "CN"),
    "湖州": ("湖州", "Huzhou", "CN"),
    "金华": ("金华", "Jinhua", "CN"),
    "舟山": ("舟山", "Zhoushan", "CN"),
    "千岛湖": ("淳安", "Chun'an", "CN"),
    "莫干山": ("德清", "Deqing", "CN"),
    "安吉": ("安吉", "Anji", "CN"),
    "临安": ("临安", "Lin'an", "CN"),
    "桐庐": ("桐庐", "Tonglu", "CN"),
}

# 城市后缀：先剥掉再查表，避免"东京市""巴黎市"查不到
_CITY_SUFFIXES = ("特别行政区", "自治区", "自治州", "地区", "都市", "市", "县", "区", "省", "州")

_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL_SECONDS = 24 * 60 * 60
_CACHE_MAX = 512


class LocationUncertain(ValueError):
    """定位到了候选，但无法确认是用户说的那个地方。"""


@dataclass(frozen=True)
class Location:
    name: str
    latitude: float
    longitude: float
    timezone: str
    country: str
    country_code: str
    english: str
    uncertain: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
            "country": self.country,
            "country_code": self.country_code,
            "english": self.english,
            "uncertain": self.uncertain,
        }


def _normalize(query: str) -> str:
    text = " ".join(str(query or "").replace("，", ",").split())
    return text.strip(" ,　")


def _strip_suffix(text: str) -> str:
    for suffix in _CITY_SUFFIXES:
        if len(text) > len(suffix) and text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def lookup_alias(query: str) -> Optional[Tuple[str, str, str]]:
    """查别名表；命中返回 (规范中文名, 英文查询名, 国家码)。"""
    text = _normalize(query)
    if not text:
        return None
    for candidate in (text, _strip_suffix(text), text.split(",")[0].strip()):
        if candidate in CITY_ALIASES:
            return CITY_ALIASES[candidate]
    return None


def _request_json(url: str, params: Dict[str, Any], timeout: float = 8.0) -> Dict[str, Any]:
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": "smartlife-agent/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {"results": payload}


def _score(result: Dict[str, Any]) -> float:
    weight = _FEATURE_WEIGHT.get(str(result.get("feature_code") or ""), 0)
    population = result.get("population") or 0
    try:
        population_score = math.log10(max(1, float(population))) * 14
    except (TypeError, ValueError):
        population_score = 0.0
    return weight + population_score


def _geocode_open_meteo(name: str, count: int = 8) -> List[Dict[str, Any]]:
    try:
        payload = _request_json(
            OPEN_METEO_GEOCODING_URL,
            {"name": name, "count": count, "language": "zh", "format": "json"},
        )
    except Exception:
        return []
    return list(payload.get("results") or [])


def _to_location(
    result: Dict[str, Any],
    english: str,
    uncertain: bool,
    display_name: str = "",
) -> Optional[Location]:
    latitude = result.get("latitude")
    longitude = result.get("longitude")
    if latitude is None or longitude is None:
        return None
    return Location(
        # 别名表命中时用规范中文名，避免把 GeoNames 的繁体/日文汉字直接甩给用户
        name=display_name or str(result.get("name") or english),
        latitude=float(latitude),
        longitude=float(longitude),
        timezone=str(result.get("timezone") or "auto"),
        country=str(result.get("country") or ""),
        country_code=str(result.get("country_code") or ""),
        english=english,
        uncertain=uncertain,
    )


def _cache_get(key: str) -> Optional[Location]:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    if time.time() - entry["at"] > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return entry["value"]


def _cache_put(key: str, value: Location) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[key] = {"at": time.time(), "value": value}


def reset_cache() -> None:
    _CACHE.clear()


def resolve_location(query: str, *, allow_uncertain: bool = True) -> Location:
    """把地名解析成坐标。

    ``allow_uncertain=False`` 时，别名表没覆盖到的地名会直接抛
    :class:`LocationUncertain`——活动/行程这类"猜错就整段作废"的用途应该用这个开关。
    """
    text = _normalize(query)
    if not text:
        raise ValueError("地名为空")

    cache_key = f"{text.casefold()}|{int(allow_uncertain)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    alias = lookup_alias(text)
    if alias is not None:
        canonical, english, country_code = alias
        # 英文名在 GeoNames 里可能带变音符（Reykjavik / Reykjavík），
        # 所以按"英文 → 中文规范名 → 原词"依次重试，但仍只接受国家匹配的候选。
        for attempt in (english, canonical, text):
            results = _geocode_open_meteo(attempt)
            matches = [
                item for item in results
                if not country_code
                or str(item.get("country_code") or "").upper() == country_code.upper()
            ]
            if not matches:
                continue
            best = max(matches, key=_score)
            location = _to_location(best, english, uncertain=False, display_name=canonical)
            if location is not None:
                _cache_put(cache_key, location)
                return location
        # 表里认得出但拿不到坐标：不再退回原词瞎猜，直接报不确定
        raise LocationUncertain(f"无法确认 {query} 的坐标（别名表命中 {english} 但无匹配候选）")

    # 表未覆盖：用原词与常见变体各查一次，按分数择优
    variants = [text, text.replace(" ", "")]
    if "," in text:
        head = text.split(",")[0].strip()
        if head and head not in variants:
            variants.append(head)
    candidates: List[Dict[str, Any]] = []
    for variant in variants:
        candidates.extend(_geocode_open_meteo(variant))
        if candidates:
            break
    if not candidates:
        raise LocationUncertain(f"无法定位地名: {query}")

    best = max(candidates, key=_score)
    location = _to_location(best, text, uncertain=True)
    if location is None:
        raise LocationUncertain(f"地理编码结果缺少坐标: {query}")
    if not allow_uncertain:
        raise LocationUncertain(
            f"地名 {query} 不在已知城市表内，最接近的候选是 {location.name}"
            f"（{location.country}），无法确认，已拒绝使用"
        )
    _cache_put(cache_key, location)
    return location


def resolve_city_english(query: str, *, allow_uncertain: bool = True) -> str:
    """城市 → 英文查询名（Photon 等英文语料接口用）。"""
    alias = lookup_alias(query)
    if alias is not None:
        return alias[1]
    location = resolve_location(query, allow_uncertain=allow_uncertain)
    return location.english or location.name


__all__ = [
    "CITY_ALIASES",
    "Location",
    "LocationUncertain",
    "lookup_alias",
    "reset_cache",
    "resolve_city_english",
    "resolve_location",
]
