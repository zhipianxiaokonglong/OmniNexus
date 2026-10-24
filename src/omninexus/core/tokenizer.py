"""Cheap, provider-independent token estimation and text helpers."""

from __future__ import annotations

import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_WHITESPACE = re.compile(r"\s+")


def estimate_tokens(text: str) -> int:
    """Approximate tokens without a model-specific tokenizer.

    CJK characters count as one token each; other text as ~4 characters per
    token. It intentionally over-estimates a little so context trimming stays
    conservative across providers.
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = max(0, len(text) - cjk)
    return max(1, cjk + (other + 3) // 4)


def estimate_message_tokens(content: str, image_count: int = 0) -> int:
    # A generic image budget; provider-specific image accounting varies.
    return estimate_tokens(content) + 4 + image_count * 850


def compact_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"
