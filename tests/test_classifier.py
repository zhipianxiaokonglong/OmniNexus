"""Classifier contract tests: deterministic local tags, no network."""

from __future__ import annotations

from omninexus.core.classifier import classify, classify_emotion, classify_type
from omninexus.core.models import ContentType, Emotion, Intent


def test_emotion_chinese_and_english() -> None:
    assert classify_emotion("太好了，非常感谢你的帮助！") == Emotion.GRATITUDE
    assert classify_emotion("I am so happy and excited, this is wonderful") == Emotion.JOY
    assert classify_emotion("这真的让我很生气，太可恶了") == Emotion.ANGER
    assert classify_emotion("普通的事实陈述，没有明显情绪。") == Emotion.NEUTRAL


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
