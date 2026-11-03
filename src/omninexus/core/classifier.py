"""Deterministic, local output classification (emotion contract v2).

The downstream expression/voice layer accepts exactly the 12 labels in
``PUBLIC_EMOTION_LABELS``. Legacy lexical groups (fear/confusion/gratitude) are
still scored for compatibility, then normalized to the public contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from omninexus.core.emotions import (
    EMOTION_CONTRACT_VERSION,
    Emotion,
    normalize_emotion,
)
from omninexus.core.models import ContentType, Intent, OutputTags

_EN_WORD = re.compile(r"[a-zA-Z']+")
_LIST_LINE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)、])\s+\S", re.M)
_TABLE_LINE = re.compile(r"^\s*\|.+\|\s*$", re.M)
_FENCE = re.compile(r"```")
_CODE_LINE = re.compile(
    r"^\s*(?:def |class |import |from \w+ import|function |const |let |var |"
    r"public |private |#include|SELECT |INSERT |UPDATE |CREATE TABLE|<\w+[^>]*>)",
    re.M,
)

# Keys are lexical groups; several are legacy labels normalized on output.
_EMOTION_LEXICON: dict[str, tuple[str, ...]] = {
    "joy": (
        "开心", "高兴", "太好了", "真棒", "喜欢", "满意", "哈哈", "愉快", "幸福", "赞",
        "happy", "glad", "great", "awesome", "delight", "love", "wonderful", "excellent",
        "🎉", "😊", "😄",
    ),
    "excitement": (
        "激动", "兴奋", "迫不及待", "热血", "燃", "excited", "thrilled", "can't wait",
        "pumped", "amazing", "🔥", "🚀",
    ),
    "comfort": (
        "别担心", "会好的", "抱抱", "安慰", "没事的", "我在", "放心", "慢慢来",
        "don't worry", "it's okay", "comfort", "take your time", "you're safe", "🫂",
    ),
    "tenderness": (
        "温柔", "心疼", "亲爱的", "宝贝", "呵护", "软软的", "乖", "么么",
        "sweetheart", "dear", "gentle", "tender", "honey", "🥰", "💗",
    ),
    "calm": (
        "冷静", "平静", "放松", "深呼吸", "慢慢", "安静", "稳定", "深呼吸一下",
        "calm", "relax", "breathe", "peaceful", "steady", "😌", "🌿",
    ),
    "sadness": (
        "难过", "伤心", "遗憾", "失望", "孤独", "哭", "可悲", "害怕", "恐惧", "担心",
        "危险", "焦虑", "不安", "sad", "unfortunately", "sorry to hear", "regret",
        "lonely", "cry", "afraid", "fear", "scared", "danger", "anxious", "worried",
        "😢", "😭", "😨",
    ),
    "apology": (
        "对不起", "抱歉", "不好意思", "我错了", "请原谅", "sorry", "apologize",
        "my apologies", "my bad", "excuse me", "🙇",
    ),
    "encouragement": (
        "加油", "你可以的", "相信你", "继续努力", "别放弃", "坚持", "做得好", "有进步",
        "you can do it", "keep going", "don't give up", "you got this", "well done",
        "proud of you", "💪",
    ),
    "surprise": (
        "没想到", "竟然", "惊讶", "意外", "居然", "surprising", "unexpected", "wow",
        "astonishing", "😮", "😲",
    ),
    # Legacy lexical group: normalized to "joy" at the public boundary.
    "gratitude": (
        "谢谢", "感谢", "多谢", "辛苦了", "thanks", "thank you", "appreciate",
        "grateful", "🙏",
    ),
    "frustration": (
        "烦", "崩溃", "无奈", "卡住", "失败", "糟糕", "不明白", "困惑", "不清楚",
        "疑问", "搞不懂", "frustrated", "annoying", "stuck", "broken", "ugh",
        "confused", "unclear", "not sure", "ambiguous", "puzzled", "😤", "🤔",
    ),
    "anger": (
        "愤怒", "生气", "可恶", "讨厌", "混蛋", "闭嘴", "angry", "furious", "hate",
        "ridiculous", "outrageous", "😠", "😡",
    ),
}

_EMOTION_WEIGHT: dict[str, float] = {
    "joy": 1.0,
    "excitement": 1.05,
    "comfort": 1.1,
    "tenderness": 1.15,
    "calm": 1.05,
    "sadness": 1.1,
    "apology": 1.2,
    "encouragement": 1.1,
    "surprise": 1.0,
    "gratitude": 1.2,
    "frustration": 1.1,
    "anger": 1.2,
}

_PRIORITY = (
    "anger",
    "frustration",
    "sadness",
    "apology",
    "comfort",
    "encouragement",
    "tenderness",
    "calm",
    "gratitude",
    "joy",
    "excitement",
    "surprise",
)


@dataclass(slots=True)
class Classification:
    tags: OutputTags


def _emotion_scores(text: str) -> dict[str, float]:
    lowered = text.lower()
    words = set(_EN_WORD.findall(lowered))
    scores: dict[str, float] = {}
    for emotion, cues in _EMOTION_LEXICON.items():
        score = 0.0
        for cue in cues:
            if cue.isascii() and cue.isalpha():
                if cue in words:
                    score += 1.0
            elif cue in lowered:
                occurrences = min(lowered.count(cue), 3)
                score += occurrences * (1.0 + min(len(cue), 6) / 20)
        if score:
            scores[emotion] = score * _EMOTION_WEIGHT[emotion]
    return scores


def classify_emotion(text: str) -> str:
    """Return one of the 12 public labels (legacy labels normalized)."""
    scores = _emotion_scores(text)
    if not scores:
        return Emotion.NEUTRAL.value
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.35:
        present = {emotion for emotion, _ in ranked[:3]}
        for emotion in _PRIORITY:
            if emotion in present:
                return normalize_emotion(emotion)
        return normalize_emotion(ranked[0][0])
    return normalize_emotion(ranked[0][0])


def classify_type(text: str) -> ContentType:
    stripped = text.strip()
    if not stripped:
        return ContentType.TEXT

    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
            return ContentType.JSON
        except (ValueError, TypeError):
            pass

    fences = len(_FENCE.findall(text))
    if fences >= 2:
        non_code_parts = re.split(r"```.*?```", text, flags=re.S)
        prose = "\n".join(non_code_parts).strip()
        code_chars = len(text) - len(prose)
        if len(prose) > 240 and code_chars < len(text) * 0.8:
            return ContentType.MIXED
        return ContentType.CODE

    lines = [line for line in text.splitlines() if line.strip()]
    table_rows = len(_TABLE_LINE.findall(text))
    if table_rows >= 3:
        return ContentType.TABLE
    if lines:
        list_rows = sum(1 for line in lines if _LIST_LINE.match(line))
        if list_rows >= 3 and list_rows / len(lines) >= 0.5:
            return ContentType.LIST
        code_rows = sum(1 for line in lines if _CODE_LINE.match(line))
        if code_rows >= 3 and code_rows / len(lines) >= 0.4:
            return ContentType.CODE
    return ContentType.TEXT


def classify(
    text: str,
    *,
    used_search: bool = False,
    used_memory: bool = False,
    fallback_used: bool = False,
) -> Classification:
    if used_search:
        intent = Intent.TOOL
    elif used_memory or fallback_used:
        intent = Intent.LINKAGE if used_memory else Intent.REPLY
    else:
        intent = Intent.REPLY
    tags = OutputTags(
        emotion=classify_emotion(text),
        type=classify_type(text).value,
        intent=intent.value,
    )
    return Classification(tags=tags)


__all__ = [
    "Classification",
    "EMOTION_CONTRACT_VERSION",
    "classify",
    "classify_emotion",
    "classify_type",
    "normalize_emotion",
]
