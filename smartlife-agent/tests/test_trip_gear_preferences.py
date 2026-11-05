"""记忆偏好的单测：只认认得出的，且只做软提示。

偏好不能压过硬约束——所以这里既测"认得出品牌"，也测"认不出就不猜"。
"""
from __future__ import annotations

from app.trip_gear.preferences import extract_preferences, price_hint


def _mem(text: str) -> dict:
    return {"content": text, "metadata": {"memory_id": "abcd1234"}}


class TestExtractPreferences:
    def test_认出品牌(self):
        prefs = extract_preferences([_mem("我买鞋只买 Salomon 和 Nike")])
        assert "Salomon" in prefs.brands
        assert "Nike" in prefs.brands

    def test_认出住宿倾向(self):
        assert extract_preferences([_mem("我一般住民宿")]).lodging == "hotel"
        assert extract_preferences([_mem("我更喜欢露营")]).lodging == "camp"

    def test_认不出来就留空(self):
        prefs = extract_preferences([_mem("今天天气不错")])
        assert prefs.empty is True

    def test_空记忆(self):
        assert extract_preferences([]).empty is True
        assert extract_preferences(["", None]).empty is True

    def test_备注写清依据(self):
        prefs = extract_preferences([_mem("我一般住民宿，鞋买 Salomon")])
        assert any("品牌偏好" in note for note in prefs.notes)
        assert any("住宿倾向" in note for note in prefs.notes)

    def test_可序列化(self):
        data = extract_preferences([_mem("我喜欢露营")]).as_dict()
        assert data["lodging"] == "camp"


class TestPriceHint:
    def test_抓出价位习惯(self):
        assert price_hint([_mem("我预算一般 1500 左右")]) == "1500"

    def test_没有就说没有(self):
        assert price_hint([_mem("随便")]) == ""
