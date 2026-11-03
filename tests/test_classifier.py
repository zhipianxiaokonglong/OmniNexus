"""Classifier contract tests: deterministic local tags, no network."""

from __future__ import annotations

from omninexus.core.classifier import classify, classify_emotion, classify_type
from omninexus.core.emotions import (
    EMOTION_CONTRACT_VERSION,
    LEGACY_EMOTION_ALIASES,
    PUBLIC_EMOTION_LABELS,
    normalize_emotion,
)
from omninexus.core.models import ContentType, Emotion, Intent


def test_emotion_chinese_and_english() -> None:
    assert classify_emotion("太好了，非常感谢你的帮助！") == Emotion.JOY.value
    assert classify_emotion("I am so happy and excited, this is wonderful") == Emotion.JOY.value
    assert classify_emotion("这真的让我很生气，太可恶了") == Emotion.ANGER.value
    assert classify_emotion("普通的事实陈述，没有明显情绪。") == Emotion.NEUTRAL.value


def test_emotion_contract_v2_labels_and_legacy_aliases() -> None:
    assert EMOTION_CONTRACT_VERSION == "2"
    assert len(PUBLIC_EMOTION_LABELS) == 12
    assert {item.value for item in Emotion} == set(PUBLIC_EMOTION_LABELS)
    assert normalize_emotion("fear") == "sadness"
    assert normalize_emotion("confusion") == "frustration"
    assert normalize_emotion("gratitude") == "joy"
    assert normalize_emotion("unknown-label") == "neutral"
    assert LEGACY_EMOTION_ALIASES == {
        "fear": "sadness",
        "confusion": "frustration",
        "gratitude": "joy",
    }
    # Legacy label in archives/output must be normalized at the boundary.
    from omninexus.core.models import OutputTags

    assert OutputTags(emotion="fear", type="text", intent="reply").emotion == "sadness"


def test_new_emotion_categories() -> None:
    assert classify_emotion("别担心，我在这里陪着你") == Emotion.COMFORT.value
    assert classify_emotion("对不起，是我的错") == Emotion.APOLOGY.value
    assert classify_emotion("加油，你可以做到的") == Emotion.ENCOURAGEMENT.value
    assert classify_emotion("冷静一下，深呼吸") == Emotion.CALM.value
    assert classify_emotion("我太激动了，迫不及待") == Emotion.EXCITEMENT.value
    assert classify_emotion("亲爱的，抱抱你") in {
        Emotion.COMFORT.value,
        Emotion.TENDERNESS.value,
    }


def test_content_types() -> None:
    assert classify_type("```python\nprint('hi')\n```") == ContentType.CODE
    assert classify_type("- one\n- two\n- three\n- four") == ContentType.LIST
    assert classify_type("| a | b |\n| --- | --- |\n| 1 | 2 |") == ContentType.TABLE
    assert classify_type('{"a": 1, "b": [2, 3]}') == ContentType.JSON
    assert classify_type("A normal sentence with no structure.") == ContentType.TEXT


def test_intent_flags() -> None:
    assert classify("hello").tags.intent == Intent.REPLY.value
    assert classify("answer", used_search=True).tags.intent == Intent.TOOL.value
    assert classify("answer", used_memory=True).tags.intent == Intent.LINKAGE.value
    assert classify("answer", used_search=True, used_memory=True).tags.intent == Intent.TOOL.value


def test_output_shape() -> None:
    result = classify("这是一段普通回答。")
    shaped = result.tags.model_dump()
    assert set(shaped) == {"emotion", "type", "intent"}
    assert shaped["emotion"] in {item.value for item in Emotion}
    assert shaped["type"] in {item.value for item in ContentType}
