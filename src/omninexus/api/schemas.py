"""HTTP request/response schemas (separate from frozen domain events)."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    title: str | None = Field(default=None, max_length=120)
    model: str | None = None
    provider_kind: str | None = Field(
        default=None, validation_alias=AliasChoices("providerKind", "provider_kind")
    )
    provider_id: str | None = Field(
        default=None, validation_alias=AliasChoices("providerId", "provider_id")
    )
    model_name: str | None = Field(
        default=None, validation_alias=AliasChoices("modelName", "model_name")
    )
    prompt_profile: str | None = None
    web_search: bool = False

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        return value[:120] or None


class SessionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    title: str | None = Field(default=None, max_length=120)
    model: str | None = None
    provider_kind: str | None = Field(
        default=None, validation_alias=AliasChoices("providerKind", "provider_kind")
    )
    provider_id: str | None = Field(
        default=None, validation_alias=AliasChoices("providerId", "provider_id")
    )
    model_name: str | None = Field(
        default=None, validation_alias=AliasChoices("modelName", "model_name")
    )
    prompt_profile: str | None = None
    web_search: bool | None = None


class ModelSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    model: str = Field(min_length=3, max_length=200)
    provider_kind: str | None = Field(
        default=None, validation_alias=AliasChoices("providerKind", "provider_kind")
    )
    provider_id: str | None = Field(
        default=None, validation_alias=AliasChoices("providerId", "provider_id")
    )
    model_name: str | None = Field(
        default=None, validation_alias=AliasChoices("modelName", "model_name")
    )


class PromptSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=100)


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    system_template: str = Field(default="", max_length=32_000)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_template: str = Field(max_length=32_000)
    change_note: str = Field(default="", max_length=500)


class VersionActivate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class PromptPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_template: str = Field(max_length=32_000)
    variables: dict[str, Any] = Field(default_factory=dict)


class MemoryProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = True
