"""Domain models shared by the API, gateway, storage and classifier layers."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omninexus.core.emotions import Emotion, normalize_emotion

__all__ = ["Emotion", "normalize_emotion"]

MAX_PARTS = 16
MAX_PART_CHARS = 12_000_000  # ~9 MB binary after base64 overhead
_SECRET_METADATA_KEY = re.compile(
    r"(api[_-]?key|access[_-]?token|auth|authorization|secret|password|credential)", re.I
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utcnow().isoformat()


class Modality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"


class ContentType(StrEnum):
    TEXT = "text"
    CODE = "code"
    LIST = "list"
    TABLE = "table"
    JSON = "json"
    MIXED = "mixed"


class Intent(StrEnum):
    REPLY = "reply"
    TOOL = "tool"
    LINKAGE = "linkage"


class OutputTags(BaseModel):
    emotion: str = Field(description="Stable emotion label, see Emotion contract v2")
    type: str = Field(description="Content type: text|code|list|table|json|mixed")
    intent: str = Field(description="reply|tool|linkage")

    @field_validator("emotion")
    @classmethod
    def _normalize_emotion(cls, value: str) -> str:
        return normalize_emotion(value)


class OutputEvent(BaseModel):
    """The frozen downstream event. Do not add required fields."""

    event_id: str
    session_id: str
    content: str
    stream: bool
    tags: OutputTags

    model_config = ConfigDict(extra="forbid")


class ContentPart(BaseModel):
    modality: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("modality")
    @classmethod
    def _normalize_modality(cls, value: str) -> str:
        value = (value or "text").strip().lower()
        aliases = {
            "txt": "text",
            "plain": "text",
            "photo": "image",
            "picture": "image",
            "img": "image",
            "voice": "audio",
            "speech": "audio",
            "document": "file",
            "attachment": "file",
            "video": "video",
        }
        return aliases.get(value, value)


class RawPart(BaseModel):
    model_config = ConfigDict(extra="allow")
    modality: str | None = None
    content: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _safe_metadata(value: Any, *, depth: int = 0) -> Any:
    """Drop credential-like metadata and bound nesting/size.

    Metadata is optional upstream decoration. Secrets must never enter storage.
    """
    if depth > 4:
        return None
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if _SECRET_METADATA_KEY.search(key):
                continue
            item = _safe_metadata(item, depth=depth + 1)
            if item is not None:
                clean[key[:128]] = item
        return clean
    if isinstance(value, list):
        return [_safe_metadata(item, depth=depth + 1) for item in value[:32]]
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:4096]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


class InboundEvent(BaseModel):
    """Flexible input event from the upstream multimodal ingestion layer.

    Canonical form:

        {"event_id": "...", "session_id": "...",
         "parts": [{"modality": "text", "content": "...", "metadata": {}}],
         "metadata": {}}

    For compatibility, a single top-level modality/content pair, a list in
    ``content`` or a ``{"parts": [...]}`` envelope are also accepted.
    """

    model_config = ConfigDict(extra="allow")

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    session_id: uuid.UUID | None = None
    modality: str | None = None
    content: Any = None
    parts: list[RawPart] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def normalized_parts(self) -> list[ContentPart]:
        raw_parts: list[RawPart] = []
        if self.parts:
            raw_parts = list(self.parts)
        else:
            content = self.content
            if isinstance(content, dict) and "parts" in content:
                content = content.get("parts")
            if isinstance(content, dict) and ("modality" in content or "content" in content):
                raw_parts = [RawPart.model_validate(content)]
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        raw_parts.append(RawPart.model_validate(item))
                    else:
                        raw_parts.append(RawPart(modality="text", content=item))
            elif content is not None:
                raw_parts = [RawPart(modality=self.modality or "text", content=content)]

        parts: list[ContentPart] = []
        for raw in raw_parts[:MAX_PARTS]:
            modality = (raw.modality or self.modality or "text").strip().lower()
            text = _as_text(raw.content)
            if len(text) > MAX_PART_CHARS:
                raise ValueError(f"content part exceeds {MAX_PART_CHARS} characters")
            if modality == "text" and not text:
                continue
            parts.append(
                ContentPart(
                    modality=modality,
                    content=text,
                    metadata=_safe_metadata(raw.metadata or {}),
                )
            )
        if not parts:
            raise ValueError("event contains no usable content parts")
        return parts

    @property
    def text(self) -> str:
        return "\n".join(
            part.content for part in self.normalized_parts() if part.modality == "text"
        )


class NormalizedInput(BaseModel):
    event_id: str
    session_id: str | None
    parts: list[ContentPart]
    metadata: dict[str, Any] = Field(default_factory=dict)
    legacy_session_id: bool = False
    session_created: bool = False

    @property
    def text(self) -> str:
        return "\n".join(part.content for part in self.parts if part.modality == "text")

    @property
    def modalities(self) -> set[str]:
        return {part.modality for part in self.parts}

    def user_preview(self, limit: int = 80) -> str:
        text = " ".join(self.text.split())
        if text:
            return text[:limit]
        if self.parts:
            return f"[{self.parts[0].modality}]"
        return "[empty]"


class StoredMessage(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    parts: list[ContentPart] = Field(default_factory=list)
    created_at: str
    token_estimate: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def new(
        cls,
        *,
        session_id: str,
        role: str,
        content: str,
        parts: list[ContentPart] | None = None,
        metadata: dict[str, Any] | None = None,
        token_estimate: int = 0,
    ) -> StoredMessage:
        return cls(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role=role,
            content=content,
            parts=parts or [],
            created_at=iso_now(),
            token_estimate=token_estimate,
            metadata=metadata or {},
        )


class SessionRecord(BaseModel):
    id: str
    title: str
    model: str | None = None
    provider_kind: str | None = None
    provider_id: str | None = None
    model_name: str | None = None
    prompt_profile: str | None = None
    web_search: bool = False
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    rank: int
    source: str = "web"


@dataclass(slots=True)
class Citation:
    url: str
    title: str
    snippet: str
    rank: int
    source: str = "web"


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(slots=True)
class LLMImage:
    url: str
    mime_type: str = "image/png"
    alt: str = ""


@dataclass(slots=True)
class LLMMessage:
    role: str
    content: str = ""
    images: list[LLMImage] = field(default_factory=list)


@dataclass(slots=True)
class ChatDelta:
    text: str
    usage: TokenUsage | None = None
    finish_reason: str | None = None


@dataclass(slots=True)
class ChatResult:
    text: str
    usage: TokenUsage
    model_id: str
    provider: str
    fallback_used: bool = False
    notes: list[str] = field(default_factory=list)


class ModelInfo(BaseModel):
    id: str
    label: str
    provider: str
    upstream_model: str
    input_modalities: list[str]
    output_modalities: list[str] = ["text"]
    context_window: int = 32_000
    max_output_tokens: int = 2048
    supports_streaming: bool = True
    supports_tools: bool = False
    available: bool = True
    unavailable_reason: str | None = None
    fallbacks: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
