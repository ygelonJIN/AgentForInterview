"""本地活动资料解析与目的地目录的一致性测试。

这两块都只吃本地数据，不联网、不调模型，属于确定性评测范围。
"""

from app.activity_notes import activity_titles, parse_activity_notes
from app.tools import geo
from app.tools.providers import PhotonPlacesProvider
from app.tools.travel_tools import _DESTINATIONS, TravelToolProvider

SAMPLE = """户外活动推荐

【露营活动】
1. 千岛湖星空露营 - 380元/人含帐篷和烧烤
   - 需提前一天预订
2. 莫干山竹林露营 - 450元/人含下午茶和早晚餐

【登山活动】
1. 黄山经典两日游 - 门票190元+索道170元
3. 武功山徒步 - 门票70元
"""


def test_parse_splits_categories_and_titles():
    notes = parse_activity_notes(SAMPLE)

    assert [note["title"] for note in notes] == [
        "千岛湖星空露营",
        "莫干山竹林露营",
        "黄山经典两日游",
        "武功山徒步",
    ]
    assert notes[0]["category"] == "露营活动"
    assert notes[2]["category"] == "登山活动"


def test_parse_merges_detail_lines():
    notes = parse_activity_notes(SAMPLE)

    assert notes[0]["detail"] == "380元/人含帐篷和烧烤；需提前一天预订"
    # 没有补充行的条目，detail 至少要有价格信息，不能空
    assert "450" in notes[1]["detail"]


def test_parse_handles_unstructured_text():
    notes = parse_activity_notes("随便一段说明\n没有任何编号条目\n")

    assert notes == []
    assert activity_titles("") == []


def test_both_readers_agree_on_the_same_file():
    """UI 目录页与工具必须数出同样多的条目。"""
    provider = TravelToolProvider()
    text = provider._read_activities("杭州")
    assert text

    tool_items = provider._activity_items(text)
    assert tool_items == activity_titles(text)
    assert len(tool_items) == len(parse_activity_notes(text))


def test_every_recommended_destination_is_geocodable():
    """推荐里出现的城市必须能被 geo 层解析，否则天气和活动都拿不到。"""
    missing = [
        item["city"] for item in _DESTINATIONS
        if geo.lookup_alias(item["city"]) is None
    ]
    assert not missing, f"目的地目录里有无法解析的地名: {missing}"


def test_destination_catalog_covers_overseas_and_ranks_by_matched_tags():
    tool = TravelToolProvider().get_tool_by_name("search_destinations")
    results = tool.invoke({
        "preference": "亲子 都市",
        "budget": 12000,
        "days": 5,
    })

    cities = [row["city"] for row in results]
    assert "新加坡" in cities or "东京" in cities
    # 命中两个标签的排在只命中一个的前面
    assert len(results[0]["matched_tags"]) == 2
    assert all(row["source"] == "local_destination_catalog" for row in results)
    assert all("示例目的地目录" in row["note"] for row in results)


def test_photon_cache_key_ignores_date(monkeypatch):
    """同一城市 5 天的行程不该把同一份场所清单请求 5 遍。"""
    provider = PhotonPlacesProvider()
    base = {"city": "新加坡", "english": "Singapore"}

    key_a = provider._cache_key({**base, "date": "2026-11-20"})
    key_b = provider._cache_key({**base, "date": "2026-11-24"})
    key_c = provider._cache_key({**base, "english": "Tokyo"})

    assert key_a == key_b
    assert key_a != key_c


def test_photon_queries_use_osm_tag_not_keyword(monkeypatch):
    """回归：q=park 这种关键词查询会把叫 Park 的酒店和商场当成公园。"""
    provider = PhotonPlacesProvider()
    seen = []

    def fake_request(url, params, **kwargs):
        seen.append(params)
        return {"features": [
            {
                "properties": {
                    "name": "National Museum",
                    "osm_key": "tourism",
                    "osm_value": "museum",
                    "city": "Singapore",
                    "country": "Singapore",
                },
                "geometry": {"coordinates": [103.848, 1.296]},
            }
        ]}

    monkeypatch.setattr("app.tools.providers._request_json", fake_request)
    payload = provider._fetch_once({"city": "新加坡", "english": "Singapore",
                                    "latitude": 1.29, "longitude": 103.85})

    assert all("osm_tag" in params for params in seen)
    assert all(params.get("q") == "Singapore" for params in seen)
    assert payload["items"][0]["kind"] == "博物馆"


def test_noisy_places_are_filtered():
    """OSM 的 tourism=attraction 很宽松，工厂和俄语标注都会混进来。"""
    from app.tools.providers import _is_noisy_place

    assert _is_noisy_place("цирк", "CN")
    # 莫斯科的西里尔名是本地名，必须保留
    assert not _is_noisy_place("цирк", "RU")
    assert _is_noisy_place("Shanghai Evergreen Printing Machinery Co.", "CN")
    assert _is_noisy_place("", "CN")
    assert _is_noisy_place("A", "CN")
    assert not _is_noisy_place("上海中心大厦", "CN")
    assert not _is_noisy_place("Μουσείο Προϊστορικής Θήρας", "GR")


def test_evidence_summary_labels_each_day():
    """每天的证据要标明第几天，否则模型只引用第一条天气。"""
    import asyncio
    from datetime import date

    from app.tools.travel_executor import TravelToolExecutor

    class _Tool:
        name = "t"

        def __init__(self, result):
            self.result = result

        def invoke(self, payload):
            return self.result

    class _Activities:
        name = "get_local_activities"

        def invoke(self, payload):
            return [{
                "city": payload["city"], "date": payload["date"],
                "activities": [{"title": "上海博物馆", "kind": "博物馆"}],
                "source": "fake", "kind": "poi", "available": True,
                "note": "", "warnings": [],
            }]

    executor = TravelToolExecutor(
        weather_tool=_Tool({"temperature": "18°C", "condition": "晴", "simulated": False}),
        route_tool=_Tool({"distance": "1公里", "duration": "5分钟", "simulated": False}),
        activities_dir="/tmp/no-activities",
        activities_tool=_Activities(),
    )

    result = asyncio.run(executor.execute(
        {"id": "day-2", "title": "第2天", "description": "市区"},
        {"requirements": {"destination": "上海", "days": 2, "start_date": "2026-11-07"}},
    ))

    assert "第2天（2026-11-08）上海天气" in result["result"]
    assert "第2天（2026-11-08）本地活动参考" in result["result"]


def test_participatory_places_rank_first_and_grouped():
    """想参加活动时，剧院/艺术中心不能被景点挤掉，且要单独成组。"""
    from app.tools.travel_executor import TravelToolExecutor

    items = [
        {"title": "上海中心大厦", "kind": "景点", "participatory": False},
        {"title": "上海博物馆", "kind": "博物馆", "participatory": False},
        {"title": "上海音乐厅", "kind": "剧院", "participatory": True},
    ]
    summary = TravelToolExecutor._activities_summary(items)

    assert "可以买票或报名参加的活动场所" in summary
    assert "另外可去的地方" in summary
    assert summary.index("上海音乐厅") < summary.index("上海中心大厦")


def test_photon_puts_participatory_first_after_truncation(monkeypatch):
    """可参与条目不足时也要排在前面，避免被景点占满 12 条配额。"""
    def fake_request(url, params, **kwargs):
        tag = params.get("osm_tag")
        first = tag[0] if isinstance(tag, list) else tag
        if "theatre" in str(tag):
            return {"features": [{
                "properties": {"name": "上海音乐厅", "osm_key": "amenity",
                               "osm_value": "theatre", "city": "Shanghai"},
                "geometry": {"coordinates": [121.47, 31.23]},
            }]}
        return {"features": [
            {
                "properties": {"name": f"景点{i}", "osm_key": "tourism",
                               "osm_value": "attraction", "city": "Shanghai"},
                "geometry": {"coordinates": [121.47, 31.23]},
            }
            for i in range(8)
        ]}

    monkeypatch.setattr("app.tools.providers._request_json", fake_request)
    payload = PhotonPlacesProvider()._fetch_once(
        {"city": "上海", "english": "Shanghai", "latitude": 31.23, "longitude": 121.47}
    )

    assert payload["items"][0]["title"] == "上海音乐厅"
    assert payload["items"][0]["participatory"] is True
    assert len(payload["items"]) <= 12
