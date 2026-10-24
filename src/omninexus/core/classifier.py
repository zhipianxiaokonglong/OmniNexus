"""Deterministic, local output classification.

The downstream expression module depends on tags, so this module is deliberately
local, fast and network-free. Labels are stable enums; changing them requires a
documented contract update.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from omninexus.core.models import (
    ContentType,
    Emotion,
    Intent,
    OutputTags,
)

_EN_WORD = re.compile(r"[a-zA-Z']+")
_LIST_LINE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)、])\s+\S", re.M)
_TABLE_LINE = re.compile(r"^\s*\|.+\|\s*$", re.M)
_FENCE = re.compile(r"```")
_CODE_LINE = re.compile(
    r"^\s*(?:def |class |import |from \w+ import|function |const |let |var |"
    r"public |private |#include|SELECT |INSERT |UPDATE |CREATE TABLE|<\w+[^>]*>)",
    re.M,
)

_EMOTION_LEXICON: dict[Emotion, tuple[str, ...]] = {
    Emotion.JOY: (
        "开心",
        "高兴",
        "太好了",
        "真棒",
        "喜欢",
        "满意",
        "哈哈",
        "愉快",
        "幸福",
        "赞",
        "happy",
        "glad",
        "great",
        "awesome",
        "delight",
        "love",
        "wonderful",
        "excellent",
        "🎉",
        "😊",
        "😄",
    ),
    Emotion.SADNESS: (
        "难过",
        "伤心",
        "遗憾",
        "失望",
        "孤独",
        "哭",
        "可悲",
        "sad",
        "unfortunately",
        "sorry to hear",
        "regret",
        "lonely",
        "cry",
        "😢",
        "😭",
    ),
    Emotion.ANGER: (
        "愤怒",
        "生气",
        "可恶",
        "讨厌",
        "混蛋",
        "闭嘴",
        "angry",
        "furious",
        "hate",
        "ridiculous",
        "outrageous",
        "😠",
        "😡",
    ),
    Emotion.FEAR: (
        "害怕",
        "恐惧",
        "担心",
        "危险",
        "焦虑",
        "不安",
        "afraid",
        "fear",
        "scared",
        "danger",
        "anxious",
        "worried",
        "😨",
    ),
    Emotion.SURPRISE: (
        "没想到",
        "竟然",
        "惊讶",
        "意外",
        "居然",
        "surprising",
        "unexpected",
        "wow",
        "amazing",
        "astonishing",
        "😮",
        "😲",
    ),
    Emotion.CONFUSION: (
        "不明白",
        "困惑",
        "不清楚",
        "疑问",
        "搞不懂",
        "confused",
        "unclear",
        "not sure",
        "ambiguous",
        "puzzled",
        "🤔",
    ),
    Emotion.FRUSTRATION: (
        "烦",
        "崩溃",
        "无奈",
        "卡住",
        "失败",
        "糟糕",
        "frustrated",
        "annoying",
        "stuck",
        "broken",
        "ugh",
        "😤",
    ),
    Emotion.GRATITUDE: (
        "谢谢",
        "感谢",
        "多谢",
        "辛苦了",
        "thanks",
        "thank you",
        "appreciate",
        "grateful",
        "🙏",
    ),
}

_EMOTION_WEIGHT = {
    Emotion.JOY: 1.0,
    Emotion.SADNESS: 1.1,
    Emotion.ANGER: 1.2,
    Emotion.FEAR: 1.1,
    Emotion.SURPRISE: 1.0,
    Emotion.CONFUSION: 0.9,
    Emotion.FRUSTRATION: 1.1,
    Emotion.GRATITUDE: 1.2,
}


@dataclass(slots=True)
class Classification:
    tags: OutputTags


def _emotion_scores(text: str) -> dict[Emotion, float]:
    lowered = text.lower()
    words = set(_EN_WORD.findall(lowered))
    scores: dict[Emotion, float] = {}
    for emotion, cues in _EMOTION_LEXICON.items():
        score = 0.0
        for cue in cues:
            if cue.isascii() and cue.isalpha():
                if cue in words:
                    score += 1.0
            elif cue in lowered:
                # Longer cues are more specific; repeated occurrences add less.
                occurrences = min(lowered.count(cue), 3)
                score += occurrences * (1.0 + min(len(cue), 6) / 20)
        if score:
            scores[emotion] = score * _EMOTION_WEIGHT[emotion]
    return scores


def classify_emotion(text: str) -> Emotion:
    scores = _emotion_scores(text)
    if not scores:
        return Emotion.NEUTRAL
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0].value))
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.35:
        # Mixed messages: gratitude/frustration commonly co-occur with others.
        priority = (
            Emotion.ANGER,
            Emotion.FRUSTRATION,
            Emotion.SADNESS,
            Emotion.FEAR,
            Emotion.GRATITUDE,
            Emotion.JOY,
            Emotion.SURPRISE,
            Emotion.CONFUSION,
        )
        present = {emotion for emotion, _ in ranked[:3]}
        for emotion in priority:
            if emotion in present:
                return emotion
        return ranked[0][0]
    return ranked[0][0]


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
        # A fallback answer still relies on local linkage/continuity context.
        intent = Intent.LINKAGE if used_memory else Intent.REPLY
    else:
        intent = Intent.REPLY
    tags = OutputTags(
        emotion=classify_emotion(text).value,
        type=classify_type(text).value,
        intent=intent.value,
    )
    return Classification(tags=tags)
