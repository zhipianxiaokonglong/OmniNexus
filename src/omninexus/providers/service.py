"""Unified view over built-in and user-managed providers."""

from __future__ import annotations

from typing import Any

from omninexus.config import AppConfig
from omninexus.core.models import SessionRecord
from omninexus.errors import ErrorCode, OmniError
from omninexus.providers.models import (
    CatalogModel,
    CustomModelRecord,
    CustomProviderSnapshot,
    ModelCapabilities,
    ProviderDefaults,
)
from omninexus.providers.store import CustomProviderStore


def split_custom_ref(ref: str) -> tuple[str, str] | None:
    if not ref.startswith("custom:"):
        return None
    parts = ref.split(":", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


class ProviderService:
    def __init__(self, config: AppConfig, store: CustomProviderStore) -> None:
        self.config = config
        self.store = store
        self._snapshots: dict[str, CustomProviderSnapshot] = {}

    async def reload(self) -> None:
        snapshots = await self.store.snapshots()
        self._snapshots = {snapshot.provider.id: snapshot for snapshot in snapshots}

    def custom_snapshots(self) -> list[CustomProviderSnapshot]:
        return list(self._snapshots.values())

    def get_custom(self, provider_id: str) -> CustomProviderSnapshot | None:
        return self._snapshots.get(provider_id)

    def custom_model(self, provider_id: str, model_name: str) -> CustomModelRecord | None:
        snapshot = self._snapshots.get(provider_id)
        if not snapshot:
            return None
        return next((model for model in snapshot.provider.models if model.name == model_name), None)

    def catalog_models(self) -> list[CatalogModel]:
        result: list[CatalogModel] = []
        for model in self.config.models:
            info = self.config.model_info(model)
            result.append(
                CatalogModel(
                    id=model.id,
                    provider_kind="builtin",
                    provider_id=model.provider,
                    provider_name=self.config.providers[model.provider].label or model.provider,
                    model=model.model,
                    label=model.label,
                    capabilities=ModelCapabilities(
                        input_modalities=list(model.input_modalities),
                        context_window=model.context_window,
                        max_output_tokens=model.max_output_tokens,
                        supports_streaming=model.supports_streaming,
                        supports_tools=model.supports_tools,
                    ),
                    verification="text_verified" if info.available else "unverified",
                    available=info.available,
                    unavailable_reason=info.unavailable_reason,
                )
            )
        for snapshot in self._snapshots.values():
            provider = snapshot.provider
            for model in provider.models:
                result.append(
                    CatalogModel(
                        id=f"custom:{provider.id}:{model.name}",
                        provider_kind="custom",
                        provider_id=provider.id,
                        provider_name=provider.name,
                        model=model.name,
                        label=model.label,
                        capabilities=model.capabilities,
                        verification=model.verification,
                        available=provider.enabled,
                        unavailable_reason=None if provider.enabled else "provider_disabled",
                    )
                )
        return result

    async def defaults(self) -> ProviderDefaults:
        stored = await self.store.defaults()
        if stored and await self.validate_target(
            stored.provider_kind, stored.provider_id, stored.model
        ):
            return stored
        ref = self.config.gateway.default_model
        if ":" in ref:
            provider_id, model = ref.split(":", 1)
            if await self.validate_target("builtin", provider_id, model):
                return ProviderDefaults(
                    provider_kind="builtin", provider_id=provider_id, model=model
                )
        # Prefer any configured model (including a configured custom provider).
        for snapshot in self._snapshots.values():
            if snapshot.provider.enabled and snapshot.provider.models:
                return ProviderDefaults(
                    provider_kind="custom",
                    provider_id=snapshot.provider.id,
                    model=snapshot.provider.default_model or snapshot.provider.models[0].name,
                )
        for model in self.config.models:
            if self.config.providers[model.provider].is_configured():
                return ProviderDefaults(
                    provider_kind="builtin",
                    provider_id=model.provider,
                    model=model.model,
                )
        return ProviderDefaults(
            provider_kind="builtin",
            provider_id=self.config.gateway.default_model.split(":", 1)[0],
            model=self.config.gateway.default_model.split(":", 1)[-1],
        )

    async def validate_target(self, provider_kind: str, provider_id: str, model_name: str) -> bool:
        if provider_kind == "custom":
            snapshot = self._snapshots.get(provider_id)
            return bool(
                snapshot
                and snapshot.provider.enabled
                and any(model.name == model_name for model in snapshot.provider.models)
            )
        model = self.config.model_config_by_id(f"{provider_id}:{model_name}")
        return model is not None and self.config.providers[provider_id].is_configured()

    async def set_defaults(self, defaults: ProviderDefaults) -> None:
        if not await self.validate_target(
            defaults.provider_kind, defaults.provider_id, defaults.model
        ):
            raise OmniError(
                ErrorCode.MODEL_UNAVAILABLE,
                "默认提供方或模型不可用，请先启用并确认模型存在。",
                status_code=422,
            )
        await self.store.set_defaults(defaults)

    def canonical_ref(self, provider_kind: str, provider_id: str, model: str) -> str:
        if provider_kind == "custom":
            return f"custom:{provider_id}:{model}"
        return f"{provider_id}:{model}"

    def resolve_ref(self, ref: str) -> tuple[str, str, str]:
        custom = split_custom_ref(ref)
        if custom:
            return "custom", custom[0], custom[1]
        if ":" in ref:
            provider_id, model = ref.split(":", 1)
            return "builtin", provider_id, model
        return "builtin", ref, ""

    async def session_availability(self, session: SessionRecord) -> tuple[bool, str | None]:
        kind = session.provider_kind
        provider_id = session.provider_id
        model_name = session.model_name
        if not kind or not provider_id or not model_name:
            if not session.model:
                return False, "no_model"
            kind, provider_id, model_name = self.resolve_ref(session.model)
        if kind == "custom":
            snapshot = self._snapshots.get(provider_id)
            if not snapshot:
                return False, "provider_deleted"
            if not snapshot.provider.enabled:
                return False, "provider_disabled"
            if not any(model.name == model_name for model in snapshot.provider.models):
                return False, "model_deleted"
            return True, None
        model = self.config.model_config_by_id(f"{provider_id}:{model_name}")
        if not model:
            return False, "model_deleted"
        provider = self.config.providers.get(provider_id)
        if not provider or not provider.enabled:
            return False, "provider_disabled"
        if not provider.is_configured():
            return False, "provider_not_configured"
        return True, None

    def provider_api_list(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": "custom",
                "id": snapshot.provider.id,
                "name": snapshot.provider.name,
                "baseUrl": snapshot.provider.base_url,
                "enabled": snapshot.provider.enabled,
                "editable": True,
                "hasApiKey": snapshot.provider.has_api_key,
                "apiKeyMasked": snapshot.provider.api_key_masked,
                "models": [model.name for model in snapshot.provider.models],
                "defaultModel": snapshot.provider.default_model,
            }
            for snapshot in self._snapshots.values()
        ]
