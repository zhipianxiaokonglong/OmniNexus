"""Camel-case API schemas for custom provider management."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ModelInput(APIModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )
    name: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=200)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class ProviderCreate(APIModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=4, max_length=2048)
    api_key: SecretStr | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    protocol: str = "openai-compatible"
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    enabled: bool = True
    models: list[ModelInput] = Field(min_length=1, max_length=200)
    default_model: str | None = None


class ProviderUpdate(APIModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=4, max_length=2048)
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    protocol: str = "openai-compatible"
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    enabled: bool = True
    models: list[ModelInput] = Field(min_length=1, max_length=200)
    default_model: str | None = None


class ProviderEnabled(APIModel):
    enabled: bool


class ProviderTestRequest(APIModel):
    id: str | None = None
    name: str | None = Field(default=None, max_length=80)
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    protocol: str = "openai-compatible"
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    model: str | None = Field(default=None, max_length=200)
    probe_multimodal: bool = False


class DiscoverModelsRequest(APIModel):
    id: str | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=10, ge=1, le=120)


class DefaultsPayload(APIModel):
    provider_kind: Literal["builtin", "custom"] = Field(
        validation_alias=AliasChoices("providerKind", "provider_kind")
    )
    provider_id: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("providerId", "provider_id"),
    )
    model: str = Field(min_length=1, max_length=200)


class ImportPayload(APIModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )
    schema_version: int = Field(default=1, alias="schemaVersion")
    providers: list[dict[str, Any]]


class MigrationApply(APIModel):
    session_ids: list[str] = Field(min_length=1, max_length=200)
    target_provider_kind: Literal["builtin", "custom"]
    target_provider_id: str
    target_model: str
