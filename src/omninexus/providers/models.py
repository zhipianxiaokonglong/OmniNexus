"""Custom provider domain models and capability presets."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SUPPORTED_MODALITIES = {"text", "image", "audio", "file"}

CAPABILITY_PRESETS: dict[str, dict[str, Any]] = {
    "text-basic": {
        "inputModalities": ["text"],
        "contextWindow": 32_000,
        "maxOutputTokens": 2048,
        "supportsStreaming": True,
        "supportsTools": False,
    },
    "text-large": {
        "inputModalities": ["text"],
        "contextWindow": 128_000,
        "maxOutputTokens": 4096,
        "supportsStreaming": True,
        "supportsTools": False,
    },
    "vision": {
        "inputModalities": ["text", "image"],
        "contextWindow": 128_000,
        "maxOutputTokens": 4096,
        "supportsStreaming": True,
        "supportsTools": False,
    },
    "local-small": {
        "inputModalities": ["text"],
        "contextWindow": 8_192,
        "maxOutputTokens": 1024,
        "supportsStreaming": True,
        "supportsTools": False,
    },
}


class ModelCapabilities(BaseModel):
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    context_window: int = 32_000
    max_output_tokens: int = 2048
    supports_streaming: bool = True
    supports_tools: bool = False

    @field_validator("input_modalities")
    @classmethod
    def _modalities(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            item = item.strip().lower()
            if item in SUPPORTED_MODALITIES and item not in cleaned:
                cleaned.append(item)
        return cleaned or ["text"]

    @field_validator("context_window")
    @classmethod
    def _context(cls, value: int) -> int:
        if value < 512:
            raise ValueError("context_window must be >= 512")
        return value

    @field_validator("max_output_tokens")
    @classmethod
    def _output(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_output_tokens must be >= 1")
        return value

    @classmethod
    def from_preset(cls, preset: str) -> ModelCapabilities:
        values = CAPABILITY_PRESETS.get(preset)
        if not values:
            raise ValueError(f"unknown capability preset: {preset}")
        return cls.model_validate(values)

    def supports(self, modality: str) -> bool:
        return modality in self.input_modalities

    def to_api(self) -> dict[str, Any]:
        return {
            "inputModalities": list(self.input_modalities),
            "contextWindow": self.context_window,
            "maxOutputTokens": self.max_output_tokens,
            "supportsStreaming": self.supports_streaming,
            "supportsTools": self.supports_tools,
        }


class CustomModelRecord(BaseModel):
    id: str
    name: str
    label: str
    capabilities: ModelCapabilities
    verification: Literal["unverified", "text_verified", "vision_verified", "failed"] = "unverified"
    last_tested_at: str | None = None


class CustomProviderRecord(BaseModel):
    id: str
    name: str
    base_url: str
    api_key_masked: str = ""
    has_api_key: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    protocol: str = "openai-compatible"
    timeout_seconds: int = 10
    enabled: bool = True
    models: list[CustomModelRecord] = Field(default_factory=list)
    default_model: str | None = None
    created_at: str
    updated_at: str


class CustomProviderSnapshot(BaseModel):
    """In-memory view used by the gateway; includes the decrypted key.

    Never serialize this model to an HTTP response or log it.
    """

    provider: CustomProviderRecord
    api_key: str | None = None


class ProviderDefaults(BaseModel):
    provider_kind: Literal["builtin", "custom"] = "builtin"
    provider_id: str
    model: str


class CatalogModel(BaseModel):
    id: str
    provider_kind: Literal["builtin", "custom"]
    provider_id: str
    provider_name: str
    model: str
    label: str
    capabilities: ModelCapabilities
    verification: str = "unverified"
    available: bool = True
    unavailable_reason: str | None = None

    @property
    def ref(self) -> str:
        if self.provider_kind == "custom":
            return f"custom:{self.provider_id}:{self.model}"
        return f"{self.provider_id}:{self.model}"

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ref": self.ref,
            "providerKind": self.provider_kind,
            "providerId": self.provider_id,
            "providerName": self.provider_name,
            "model": self.model,
            "label": self.label,
            "capabilities": self.capabilities.to_api(),
            "verification": self.verification,
            "available": self.available,
            "unavailableReason": self.unavailable_reason,
        }
