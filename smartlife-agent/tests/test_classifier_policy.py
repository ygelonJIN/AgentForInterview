"""分类强校验、置信度仲裁和任务切换策略测试。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.classifier import (
    ClassificationResponseError,
    UnifiedClassifier,
    _keyword_classify,
    _parse_json_response,
)


def _payload(confidence=0.8, intent="shopping", retrieval="mixed", sub_intent="search"):
    return {
        "route": "react",
        "intent": intent,
        "sub_intent": sub_intent,
        "retrieval": retrieval,
        "confidence": confidence,
        "reason": "test",
    }


class _Chain:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def invoke(self, values):
        self.calls.append(values)
        return SimpleNamespace(content=self.outputs.pop(0))

    async def ainvoke(self, values):
        self.calls.append(values)
        return SimpleNamespace(content=self.outputs.pop(0))


def _classifier(primary, arbiter=None):
    classifier = UnifiedClassifier.__new__(UnifiedClassifier)
    classifier._chain = _Chain(primary)
    classifier._arbiter_chain = _Chain(arbiter or [])
    return classifier


def test_json_parser_supports_nested_objects_and_code_fences():
    content = '```json\n{"result":{"kind":"nested"},"confidence":0.7}\n```'
    assert _parse_json_response(content)["result"] == {"kind": "nested"}


def test_json_parser_rejects_missing_visible_json():
    with pytest.raises(ClassificationResponseError):
        _parse_json_response("我暂时无法返回分类")


def test_missing_required_field_is_rejected_before_fallback():
    classifier = _classifier([json.dumps({"intent": "shopping"})])
    with pytest.raises(Exception):
        classifier._from_data(
            _parse_json_response(classifier.chain.invoke({}).content)
        )


def test_confidence_must_be_numeric_not_string_or_boolean():
    classifier = _classifier([])
    for value in ("0.8", True):
        payload = _payload()
        payload["confidence"] = value
        with pytest.raises(ClassificationResponseError):
            classifier._from_data(payload)


def test_low_confidence_small_model_result_is_arbitrated_by_main_model(monkeypatch):
    monkeypatch.setattr("app.classifier._keyword_classify", lambda _message: None)
    low = json.dumps(_payload(confidence=0.4, intent="general", retrieval="none"))
    arbitrated = json.dumps(_payload(confidence=0.82, intent="shopping"))
    classifier = _classifier([low], [arbitrated])

    result = classifier.classify("想找一双适合跑步的鞋")

    assert result.intent == "shopping"
    assert classifier._arbiter_chain.calls[0]["mode"] == "low_confidence"


def test_invalid_small_model_output_is_repaired_once(monkeypatch):
    monkeypatch.setattr("app.classifier._keyword_classify", lambda _message: None)
    repaired = json.dumps(_payload(intent="travel", retrieval="rag_only", sub_intent="qa"))
    classifier = _classifier(["没有 JSON"], [repaired])

    result = classifier.classify("杭州有什么好玩的")

    assert result.intent == "travel"
    assert classifier._arbiter_chain.calls[0]["mode"] == "repair"


def test_async_low_confidence_uses_arbitration():
    low = json.dumps(_payload(confidence=0.2, intent="general", retrieval="none"))
    arbitrated = json.dumps(_payload(confidence=0.76, intent="travel", retrieval="rag_only", sub_intent="qa"))
    classifier = _classifier([low], [arbitrated])

    result = asyncio.run(classifier.aclassify("周末想去杭州玩"))

    assert result.intent == "travel"


def test_arbitration_failure_keeps_valid_low_confidence_candidate():
    low = json.dumps(_payload(confidence=0.4, intent="general", retrieval="none"))

    class _BrokenChain:
        def invoke(self, _values):
            raise RuntimeError("arbiter unavailable")

    classifier = _classifier([low])
    classifier._arbiter_chain = _BrokenChain()

    result = classifier.classify("帮我处理一下")

    assert result.intent == "general"
    assert result.confidence == 0.4
    assert "仲裁失败" in result.reason


def test_strong_new_task_signal_blocks_stale_context_reuse():
    from app.classifier import ClassificationResult

    previous = ClassificationResult(
        route="plan_and_execute",
        intent="travel",
        sub_intent="plan_trip",
        retrieval="rag_only",
        confidence=0.8,
    )
    classifier = _classifier([])

    assert classifier._context_fallback("改成手机", previous) is None


def test_keyword_classifier_keeps_explicit_shopping_and_travel_multi_intent():
    result = _keyword_classify(
        "给我推荐一双800元的跑鞋，再给我一份杭州玩两天的计划"
    )

    assert result is not None
    assert set(result["intents"]) == {"shopping", "travel"}
    assert result["intent"] in {"shopping", "travel"}
    assert result["route"] == "plan_and_execute"
    assert result["intent_scores"]["shopping"] >= 2
    assert result["intent_scores"]["travel"] >= 2
    assert result["retrieval"] == "mixed"


def test_small_model_schema_accepts_two_intents_and_keeps_primary():
    classifier = _classifier([])
    payload = _payload(intent="travel", retrieval="mixed", sub_intent="search+plan_trip")
    payload.update({
        "intents": ["travel", "shopping"],
        "primary_intent": "travel",
        "intent_scores": {"travel": 0.91, "shopping": 0.88},
    })

    result = classifier._from_data(payload)

    assert result.intent == "travel"
    assert result.effective_intents == ["travel", "shopping"]
    assert result.intent_scores == {"travel": 0.91, "shopping": 0.88}


def test_small_model_schema_rejects_more_than_two_intents():
    classifier = _classifier([])
    payload = _payload()
    payload["intents"] = ["shopping", "travel", "customer_service"]

    with pytest.raises(ClassificationResponseError):
        classifier._from_data(payload)


def test_two_weak_shopping_words_do_not_trigger_fast_shopping_route():
    assert _keyword_classify("价格多少钱") is None


def test_current_message_keyword_evidence_is_not_supplemented_by_history():
    classifier = _classifier([
        json.dumps(_payload(confidence=0.7, intent="shopping"))
    ])

    result = classifier.classify(
        "第二个呢",
        history=[{"role": "user", "content": "推荐一款手机"}],
    )

    assert result.intent == "shopping"
    assert "关键词" not in result.reason
    assert classifier._chain.calls[0]["context"] == "user: 推荐一款手机"
