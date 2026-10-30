"""HTTP API for user-managed OpenAI-compatible providers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from omninexus.api.provider_schemas import (
    DefaultsPayload,
    DiscoverModelsRequest,
    ImportPayload,
    MigrationApply,
    ProviderCreate,
    ProviderEnabled,
    ProviderTestRequest,
    ProviderUpdate,
)
from omninexus.errors import ErrorCode, OmniError
from omninexus.providers.models import (
    CAPABILITY_PRESETS,
    CustomProviderRecord,
    ProviderDefaults,
)
from omninexus.providers.service import ProviderService
from omninexus.providers.store import CustomProviderStore
from omninexus.providers.tester import discover_models, test_provider

router = APIRouter(prefix="/v1")


def _service(request: Request) -> ProviderService:
    return request.app.state.provider_service


def _store(request: Request) -> CustomProviderStore:
    return request.app.state.provider_store


def _ensure_protocol(protocol: str) -> None:
    if protocol != "openai-compatible":
        raise OmniError(
            ErrorCode.UNSUPPORTED_PROTOCOL,
            "当前仅支持 OpenAI Compatible 接口。",
            status_code=422,
        )


async def _refresh(request: Request) -> None:
    service = _service(request)
    await service.reload()
    await request.app.state.gateway.refresh_custom_providers()


def _provider_api(provider: CustomProviderRecord) -> dict[str, Any]:
    return {
        "id": provider.id,
        "name": provider.name,
        "baseUrl": provider.base_url,
        "apiKeyMasked": provider.api_key_masked,
        "hasApiKey": provider.has_api_key,
        "headers": provider.headers,
        "protocol": provider.protocol,
        "timeoutSeconds": provider.timeout_seconds,
        "enabled": provider.enabled,
        "models": [
            {
                "name": model.name,
                "label": model.label,
                "capabilities": model.capabilities.to_api(),
                "verification": model.verification,
                "lastTestedAt": model.last_tested_at,
            }
            for model in provider.models
        ],
        "defaultModel": provider.default_model,
        "createdAt": provider.created_at,
        "updatedAt": provider.updated_at,
    }


def _models_payload(body: ProviderCreate | ProviderUpdate) -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "label": item.label,
            "capabilities": item.capabilities,
        }
        for item in body.models
    ]


def _headers(body: Any) -> dict[str, str]:
    return {str(key): str(value) for key, value in (body.headers or {}).items()}


@router.get("/custom-providers")
async def list_custom_providers(request: Request) -> dict[str, Any]:
    providers = await _store(request).list()
    return {
        "providers": [_provider_api(provider) for provider in providers],
        "capabilityPresets": CAPABILITY_PRESETS,
        "storageWarning": (
            "API Key 加密存储于本机 data/ 下；能访问本机账户的人仍可能读取。"
            "桌面版将接入系统钥匙串。"
        ),
    }


@router.get("/custom-providers/{provider_id}")
async def get_custom_provider(request: Request, provider_id: str) -> dict[str, Any]:
    return {"provider": _provider_api(await _store(request).get(provider_id))}


@router.post("/custom-providers", status_code=201)
async def create_custom_provider(request: Request, body: ProviderCreate) -> dict[str, Any]:
    provider = await _store(request).create(
        name=body.name,
        base_url=body.base_url,
        api_key=body.api_key.get_secret_value() if body.api_key else None,
        headers=_headers(body),
        protocol=body.protocol,
        timeout_seconds=body.timeout_seconds,
        enabled=body.enabled,
        models=_models_payload(body),
        default_model=body.default_model,
    )
    await _refresh(request)
    return {"provider": _provider_api(provider)}


@router.put("/custom-providers/{provider_id}")
async def update_custom_provider(
    request: Request, provider_id: str, body: ProviderUpdate
) -> dict[str, Any]:
    api_key = body.api_key.get_secret_value() if body.api_key is not None else None
    update_key = body.clear_api_key or body.api_key is not None
    provider = await _store(request).update(
        provider_id,
        name=body.name,
        base_url=body.base_url,
        headers=_headers(body),
        protocol=body.protocol,
        timeout_seconds=body.timeout_seconds,
        enabled=body.enabled,
        models=_models_payload(body),
        default_model=body.default_model,
        api_key_update=api_key,
        update_key=update_key,
    )
    await _refresh(request)
    return {"provider": _provider_api(provider)}


@router.patch("/custom-providers/{provider_id}/enabled")
async def set_custom_provider_enabled(
    request: Request, provider_id: str, body: ProviderEnabled
) -> dict[str, Any]:
    store = _store(request)
    if not body.enabled and await store.is_default(provider_id):
        raise OmniError(
            ErrorCode.DEFAULT_PROVIDER_REQUIRED,
            "该提供方是当前默认提供方，请先切换默认提供方/模型。",
            status_code=409,
        )
    provider = await store.set_enabled(provider_id, body.enabled)
    await _refresh(request)
    return {"provider": _provider_api(provider)}


@router.delete("/custom-providers/{provider_id}", status_code=204)
async def delete_custom_provider(
    request: Request,
    provider_id: str,
    confirm: bool = Query(default=False),
) -> None:
    store = _store(request)
    await store.get(provider_id)
    if await store.is_default(provider_id):
        raise OmniError(
            ErrorCode.DEFAULT_PROVIDER_REQUIRED,
            "该提供方是当前默认提供方，请先切换默认提供方/模型。",
            status_code=409,
        )
    usage = await store.usage_count(provider_id)
    if usage and not confirm:
        raise OmniError(
            ErrorCode.PROVIDER_IN_USE,
            f"该提供方被 {usage} 个会话使用；确认后历史会话会保留并标记不可用。",
            status_code=409,
            details={"usageCount": usage, "requiresConfirmation": True},
        )
    await store.delete(provider_id)
    await _refresh(request)


@router.post("/custom-providers/test")
async def test_unsaved_provider(request: Request, body: ProviderTestRequest) -> dict[str, Any]:
    _ensure_protocol(body.protocol)
    api_key = body.api_key.get_secret_value() if body.api_key is not None else None
    if body.id and not api_key:
        snapshot = _service(request).get_custom(body.id)
        if snapshot:
            api_key = snapshot.api_key
    if not body.base_url:
        raise OmniError(ErrorCode.INVALID_INPUT, "Base URL 不能为空。", status_code=422)
    result = await test_provider(
        base_url=body.base_url,
        api_key=api_key,
        headers=_headers(body),
        timeout_seconds=body.timeout_seconds,
        model=body.model,
        probe_multimodal=body.probe_multimodal,
        transport=request.app.state.provider_transport,
    )
    # Unsaved tests must not mark the persisted provider as verified: the
    # request may contain a different Base URL/key than the saved config.
    return {"result": result}


@router.post("/custom-providers/{provider_id}/test")
async def test_saved_provider(
    request: Request, provider_id: str, body: ProviderTestRequest
) -> dict[str, Any]:
    _ensure_protocol(body.protocol)
    provider = await _store(request).get(provider_id)
    snapshot = _service(request).get_custom(provider_id)
    api_key = body.api_key.get_secret_value() if body.api_key is not None else None
    if api_key is None and snapshot:
        api_key = snapshot.api_key
    result = await test_provider(
        base_url=body.base_url or provider.base_url,
        api_key=api_key,
        headers=_headers(body) or provider.headers,
        timeout_seconds=body.timeout_seconds,
        model=body.model,
        probe_multimodal=body.probe_multimodal,
        transport=request.app.state.provider_transport,
    )
    if body.model:
        await _record_verification(request, provider_id, body.model, result)
    return {"result": result}


@router.post("/custom-providers/discover-models")
async def discover_unsaved_models(request: Request, body: DiscoverModelsRequest) -> dict[str, Any]:
    api_key = body.api_key.get_secret_value() if body.api_key is not None else None
    if body.id and not api_key:
        snapshot = _service(request).get_custom(body.id)
        if snapshot:
            api_key = snapshot.api_key
    if not body.base_url:
        raise OmniError(ErrorCode.INVALID_INPUT, "Base URL 不能为空。", status_code=422)
    models = await discover_models(
        base_url=body.base_url,
        api_key=api_key,
        headers=_headers(body),
        timeout_seconds=body.timeout_seconds,
        transport=request.app.state.provider_transport,
    )
    return {"models": models, "unverified": True}


@router.post("/custom-providers/{provider_id}/discover-models")
async def discover_saved_models(
    request: Request, provider_id: str, body: DiscoverModelsRequest
) -> dict[str, Any]:
    provider = await _store(request).get(provider_id)
    snapshot = _service(request).get_custom(provider_id)
    api_key = body.api_key.get_secret_value() if body.api_key is not None else None
    if api_key is None and snapshot:
        api_key = snapshot.api_key
    models = await discover_models(
        base_url=body.base_url or provider.base_url,
        api_key=api_key,
        headers=_headers(body) or provider.headers,
        timeout_seconds=body.timeout_seconds,
        transport=request.app.state.provider_transport,
    )
    return {"models": models, "unverified": True}


async def _record_verification(
    request: Request, provider_id: str, model: str, result: dict[str, Any]
) -> None:
    status = result.get("status")
    multimodal = result.get("multimodal") or {}
    if status == "success":
        verification = (
            "vision_verified" if multimodal.get("status") == "verified" else "text_verified"
        )
    elif status in {"auth_failed", "unreachable", "timeout", "model_not_found"}:
        verification = "failed"
    else:
        verification = "unverified"
    await _store(request).set_model_verification(provider_id, model, verification)
    await _refresh(request)


@router.get("/provider-defaults")
async def get_provider_defaults(request: Request) -> dict[str, Any]:
    service = _service(request)
    defaults = await service.defaults()
    provider_name = defaults.provider_id
    if defaults.provider_kind == "custom":
        snapshot = service.get_custom(defaults.provider_id)
        provider_name = snapshot.provider.name if snapshot else "已删除"
    return {
        "defaults": {
            "providerKind": defaults.provider_kind,
            "providerId": defaults.provider_id,
            "providerName": provider_name,
            "model": defaults.model,
        }
    }


@router.put("/provider-defaults")
async def set_provider_defaults(request: Request, body: DefaultsPayload) -> dict[str, Any]:
    service = _service(request)
    defaults = ProviderDefaults(
        provider_kind=body.provider_kind,
        provider_id=body.provider_id,
        model=body.model,
    )
    await service.set_defaults(defaults)
    return {
        "defaults": {
            "providerKind": defaults.provider_kind,
            "providerId": defaults.provider_id,
            "model": defaults.model,
        }
    }


@router.get("/provider-config/export")
async def export_provider_config(request: Request) -> JSONResponse:
    payload = await _store(request).export_config()
    return JSONResponse(
        payload,
        headers={
            "Content-Disposition": 'attachment; filename="omninexus-providers.json"',
            "X-OmniNexus-Export": "providers-no-secrets",
        },
    )


@router.post("/provider-config/import")
async def import_provider_config(request: Request, body: ImportPayload) -> dict[str, Any]:
    created = await _store(request).import_config(
        {"schemaVersion": body.schema_version, "providers": body.providers}
    )
    await _refresh(request)
    return {
        "imported": len(created),
        "providers": [_provider_api(provider) for provider in created],
        "note": "API Key 未导入；提供方已禁用，请重新填写 Key 并测试后再启用。",
    }


@router.get("/provider-migration/candidates")
async def migration_candidates(request: Request) -> dict[str, Any]:
    sessions = await request.app.state.sessions.list(limit=200)
    items = [
        {
            "id": item["id"],
            "title": item.get("title"),
            "model": item.get("model"),
            "reason": item.get("providerUnavailableReason"),
            "updatedAt": item.get("updated_at"),
        }
        for item in sessions
        if not item.get("providerAvailable", True)
    ]
    return {"sessions": items, "total": len(items)}


@router.post("/provider-migration/apply")
async def apply_migration(request: Request, body: MigrationApply) -> dict[str, Any]:
    service = _service(request)
    if not await service.validate_target(
        body.target_provider_kind, body.target_provider_id, body.target_model
    ):
        raise OmniError(
            ErrorCode.MODEL_UNAVAILABLE,
            "目标提供方或模型不可用。",
            status_code=422,
        )
    ref = service.canonical_ref(
        body.target_provider_kind, body.target_provider_id, body.target_model
    )
    updated = 0
    for session_id in body.session_ids[:200]:
        try:
            await request.app.state.sessions.update(
                session_id,
                {
                    "model": ref,
                    "provider_kind": body.target_provider_kind,
                    "provider_id": body.target_provider_id,
                    "model_name": body.target_model,
                },
            )
            updated += 1
        except OmniError:
            continue
    return {"updated": updated, "model": ref}
