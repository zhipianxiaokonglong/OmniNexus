"""Frozen wire contract for the reserved external-memory extension (v1.0).

This adapter is NOT part of the v1.1 delivery pipeline. It is kept as a stable,
default-disabled extension point. Do not rename paths, fields or error codes
without a joint contract revision.

Health:  GET  {base_url}/health
Query:   POST {base_url}/v1/memory/query
Write:   POST {base_url}/v1/memory/write

Every request carries ``X-OmniNexus-Memory-Contract: 1.0``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MEMORY_CONTRACT_VERSION = "1.0"
CONTRACT_HEADER = "X-OmniNexus-Memory-Contract"

ERROR_CODES: dict[str, int] = {
    "MEM_OK": 200,
    "MEM_BAD_REQUEST": 400,
    "MEM_UNAUTHORIZED": 401,
    "MEM_RATE_LIMITED": 429,
    "MEM_TIMEOUT": 504,
    "MEM_UNAVAILABLE": 503,
    "MEM_INTERNAL": 500,
    "MEM_SCHEMA": 422,
}

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "what",
    "when",
    "where",
    "which",
    "who",
    "how",
    "why",
    "are",
    "was",
    "were",
    "you",
    "your",
    "我们的",
    "这个",
    "那个",
    "什么",
    "怎么",
    "如何",
    "可以",
    "请问",
    "一下",
    "帮我",
    "我想",
    "是否",
    "然后",
    "因为",
    "所以",
    "但是",
    "如果",
    "以及",
}
_CUES = re.compile(
    r"(记得|还记得|之前|上次|以前|曾经|历史|回忆|remember|previously|last time|before)",
    re.I,
)
_TIME = re.compile(
    r"(今天|昨日|昨天|前天|上周|本周|这周|最近\s*(\d+)\s*天|近\s*(\d+)\s*天|"
    r"today|yesterday|last week|this week|last\s+(\d+)\s+days)",
    re.I,
)
_ENTITY = re.compile(
    r"(“[^”]{1,40}”|\"[^\"]{1,40}\"|《[^》]{1,40}》|@[A-Za-z0-9_]{2,32}|"
    r"\b[A-Z][A-Za-z0-9_.+-]{2,40}\b|https?://[^\s<>]{4,300})"
)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,}")


class MemoryQuery(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    time_range: dict[str, str] | None = None
    top_k: int = 5
    priority: Literal["high", "normal"] = "normal"


class MemoryQueryEnvelope(BaseModel):
    query: MemoryQuery


class MemoryItem(BaseModel):
    id: str = ""
    content: str
    score: float = 0.0
    source: str = "memory"
    timestamp: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def _bound(cls, value: str) -> str:
        return value[:4000]


class MemoryQueryResponse(BaseModel):
    contract_version: str = MEMORY_CONTRACT_VERSION
    items: list[MemoryItem] = Field(default_factory=list)
    tokens: int = 0


def _time_range(text: str, now: datetime | None = None) -> dict[str, str] | None:
    now = now or datetime.now().astimezone()
    match = _TIME.search(text)
    if not match:
        return None
    cue = match.group(0).lower()
    start: datetime
    end = now
    if "今天" in cue or "today" in cue:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif "昨天" in cue or "昨日" in cue or "yesterday" in cue:
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
    elif "前天" in cue:
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        start = end - timedelta(days=1)
    elif "上周" in cue or "last week" in cue:
        start = now - timedelta(days=14)
    elif "本周" in cue or "这周" in cue or "this week" in cue:
        start = now - timedelta(days=7)
    else:
        days_match = re.search(r"(\d+)", cue)
        days = int(days_match.group(1)) if days_match else 7
        start = now - timedelta(days=min(days, 3650))
    return {"start": start.isoformat(), "end": end.isoformat()}


def build_query(text: str, *, top_k: int = 5, now: datetime | None = None) -> dict[str, Any]:
    text = " ".join(text.split())[:4000]
    keywords: list[str] = []
    for run in _CJK_RUN.findall(text):
        for size in (2, 3, 4):
            for index in range(0, min(len(run) - size + 1, 8)):
                token = run[index : index + size]
                if token not in _STOPWORDS:
                    keywords.append(token)
    keywords.extend(word for word in _WORD.findall(text) if word.lower() not in _STOPWORDS)
    seen: set[str] = set()
    unique_keywords: list[str] = []
    for word in keywords:
        key = word.lower()
        if key not in seen:
            seen.add(key)
            unique_keywords.append(word[:64])
        if len(unique_keywords) >= 10:
            break

    entities: list[str] = []
    entity_seen: set[str] = set()
    for entity in _ENTITY.findall(text):
        entity = entity.strip()
        if entity and entity.lower() not in entity_seen:
            entity_seen.add(entity.lower())
            entities.append(entity[:300])
        if len(entities) >= 8:
            break

    return {
        "query": {
            "keywords": unique_keywords,
            "entities": entities,
            "time_range": _time_range(text, now),
            "top_k": max(1, min(top_k, 10)),
            "priority": "high" if _CUES.search(text) else "normal",
        }
    }
