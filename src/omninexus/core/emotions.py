"""Emotion tag contract v2.

The downstream voice/expression layer accepts exactly these 12 labels. Legacy
labels emitted by older OmniNexus builds are normalized at the boundary so old
archives and third-party integrations keep working.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

EMOTION_CONTRACT_VERSION = "2"

PUBLIC_EMOTION_LABELS: tuple[str, ...] = (
    "neutral",
    "joy",
    "excitement",
    "comfort",
    "tenderness",
    "calm",
    "sadness",
    "apology",
    "encouragement",
    "surprise",
    "frustration",
    "anger",
)

LEGACY_EMOTION_ALIASES: dict[str, str] = {
    "fear": "sadness",
    "confusion": "frustration",
    "gratitude": "joy",
}


class Emotion(StrEnum):
    NEUTRAL = "neutral"
    JOY = "joy"
    EXCITEMENT = "excitement"
    COMFORT = "comfort"
    TENDERNESS = "tenderness"
    CALM = "calm"
    SADNESS = "sadness"
    APOLOGY = "apology"
    ENCOURAGEMENT = "encouragement"
    SURPRISE = "surprise"
    FRUSTRATION = "frustration"
    ANGER = "anger"


def normalize_emotion(value: str | None) -> str:
    """Map any legacy/unknown value to the 12-label public contract."""
    label = str(value or "").strip().lower()
    if label in PUBLIC_EMOTION_LABELS:
        return label
    if label in LEGACY_EMOTION_ALIASES:
        return LEGACY_EMOTION_ALIASES[label]
    return Emotion.NEUTRAL.value


def tag_contract() -> dict[str, Any]:
    return {
        "version": EMOTION_CONTRACT_VERSION,
        "emotion": {
            "labels": list(PUBLIC_EMOTION_LABELS),
            "legacy_aliases": dict(LEGACY_EMOTION_ALIASES),
        },
    }


# Third-party callers validate these. `service_version` is separate on purpose:
# contract versions only change on breaking wire changes, while the package
# version changes on every release.
EVENT_CONTRACT_VERSION = "1.0"
ERROR_CONTRACT_VERSION = "1.0"


def contract_manifest(service_version: str, memory_contract_version: str) -> dict[str, Any]:
    """Machine-readable contract surface for downstream integrators."""
    return {
        "service": "omninexus",
        "service_version": service_version,
        "contracts": {
            "event": {
                "version": EVENT_CONTRACT_VERSION,
                "required_fields": ["event_id", "session_id", "content", "stream", "tags"],
                "tag_fields": ["emotion", "type", "intent"],
                "content_types": ["text", "code", "list", "table", "json", "mixed"],
                "intents": ["reply", "tool", "linkage"],
            },
            "emotion": {
                "version": EMOTION_CONTRACT_VERSION,
                "labels": list(PUBLIC_EMOTION_LABELS),
                "legacy_aliases": dict(LEGACY_EMOTION_ALIASES),
            },
            "memory": {
                "version": memory_contract_version,
                "status": "reserved",
                "default_enabled": False,
            },
            "error": {
                "version": ERROR_CONTRACT_VERSION,
                "fields": ["code", "message", "retryable", "details", "request_id"],
                "request_id_header": "X-Request-ID",
            },
        },
        "compatibility": {
            "policy": "major_reject_minor_degrade",
            "description": (
                "Consumers should reject an incompatible contract major version, "
                "and degrade with a warning for additive minor changes."
            ),
        },
    }
