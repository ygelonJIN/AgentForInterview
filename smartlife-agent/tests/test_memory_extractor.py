"""候选记忆清洗、去重和噪声过滤测试。"""

from types import SimpleNamespace

from app.memory.extractor import MemoryExtractor


class _Chain:
    def __init__(self, content):
        self.content = content

    def invoke(self, _values):
        return SimpleNamespace(content=self.content)


def _extractor():
    extractor = MemoryExtractor.__new__(MemoryExtractor)
    extractor.chain = _Chain("[]")
    return extractor


def test_clean_candidates_dedupes_and_filters_existing_memories():
    extractor = _extractor()
    candidates = [
        {"content": "用户预算 1000 元", "category": "shopping", "confidence": "high"},
        {"content": "用户预算1000元", "category": "shopping", "confidence": "high"},
        {"content": "用户预算 1000 元", "category": "general", "confidence": "low"},
        {"content": "已存在事实", "category": "general", "confidence": "high"},
        {"content": "我觉得可能喜欢蓝色", "category": "general", "confidence": "low"},
        {"content": "短", "category": "general", "confidence": "low"},
        {"content": "无效类别但可保留", "category": "unknown", "confidence": "unknown"},
    ]

    result = extractor._clean_candidates(
        candidates,
        existing_contents=["用户预算 1000 元", "已存在事实"],
        excluded_contents=["无效类别但可保留"],
    )

    assert result == []


def test_clean_candidates_caps_output_and_normalizes_whitespace():
    extractor = _extractor()
    candidates = [{"content": f"事实 {i}  ", "category": "general"} for i in range(15)]

    result = extractor._clean_candidates(candidates)

    assert len(result) == 10
    assert result[0]["content"] == "事实 0"


def test_extract_candidates_uses_cleaning_and_diag_reports_counts():
    extractor = _extractor()
    extractor.chain = _Chain(
        '[{"content":"事实A","category":"general","confidence":"high"},'
        '{"content":"事实A","category":"general","confidence":"high"}]'
    )

    candidates = extractor.extract_candidates(
        [{"role": "user", "content": "事实A"}],
        existing_contents=[],
    )
    cleaned, diag = extractor.extract_candidates_with_diag(
        [{"role": "user", "content": "事实A"}],
        existing_contents=[],
    )

    assert candidates == [{
        "content": "事实A", "category": "general", "confidence": "high", "durability": "durable",
    }]
    assert cleaned == candidates
    assert "候选 2 条，去重过滤后 1 条" in diag


def test_empty_extraction_result_is_not_displayed():
    extractor = _extractor()
    extractor.chain = _Chain("[]")

    result = extractor.extract_candidates_result([{"role": "user", "content": "你好"}])

    assert result.status == "no_content"
    assert result.candidates == []
    assert result.should_display is False


def test_invalid_extraction_output_is_reported_as_failure():
    extractor = _extractor()
    extractor.chain = _Chain("模型没有按照要求返回 JSON")

    result = extractor.extract_candidates_result([{"role": "user", "content": "事实A"}])

    assert result.status == "error"
    assert result.has_error is True
    assert result.should_display is True
    assert "无法解析" in result.diagnostic


def test_extraction_exception_is_reported_as_failure():
    class _BrokenChain:
        def invoke(self, _values):
            raise RuntimeError("small model unavailable")

    extractor = _extractor()
    extractor.chain = _BrokenChain()

    result = extractor.extract_candidates_result([{"role": "user", "content": "事实A"}])

    assert result.status == "error"
    assert result.should_display is True
    assert "small model unavailable" in result.diagnostic


class TestDurability:
    """一次性行程事实不该每轮都弹确认，长期偏好才值得记。"""

    def test_一次性行程事实标为临时(self):
        from app.memory.extractor import MemoryExtractor

        assert MemoryExtractor._durability("目的地为杭州") == "transient"
        assert MemoryExtractor._durability("预算为5000元") == "transient"
        assert MemoryExtractor._durability("周末去莫干山露营") == "transient"

    def test_稳定偏好标为长期(self):
        from app.memory.extractor import MemoryExtractor

        assert MemoryExtractor._durability("我喜欢弹吉他") == "durable"
        assert MemoryExtractor._durability("我一般住民宿") == "durable"
        assert MemoryExtractor._durability("我对花生过敏") == "durable"

    def test_默认过滤一次性事实(self):
        extractor = _extractor()
        extractor.chain = _Chain(
            '[{"content":"目的地为杭州","category":"travel","confidence":"high"},'
            '{"content":"我喜欢弹吉他","category":"general","confidence":"high"}]'
        )
        result = extractor.extract_candidates_result([{"role": "user", "content": "去杭州"}])
        assert [c["content"] for c in result.candidates] == ["我喜欢弹吉他"]

    def test_显式要求时才保留一次性事实(self):
        extractor = _extractor()
        extractor.chain = _Chain(
            '{"candidates":[{"content":"目的地为杭州","category":"travel","confidence":"high"}]}'
        )
        result = extractor.extract_candidates_result(
            [{"role": "user", "content": "去杭州"}], include_transient=True
        )
        assert [c["content"] for c in result.candidates] == ["目的地为杭州"]

    def test_兜底事实默认不弹(self):
        # 模型没输出时，正则兜底抽出的就是"目的地/天数/预算"这三类，
        # 它们全是临时事实，默认应当一条都不提示。
        extractor = _extractor()
        extractor.chain = _Chain("")
        result = extractor.extract_candidates_result(
            [{"role": "user", "content": "周末去杭州玩两天，预算5000"}]
        )
        assert result.candidates == []
        assert result.status == "no_content"
