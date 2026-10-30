"""Persistence and validation for user-managed providers/models."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from omninexus.core.models import iso_now
from omninexus.errors import ErrorCode, OmniError
from omninexus.providers.models import (
    CustomModelRecord,
    CustomProviderRecord,
    CustomProviderSnapshot,
    ModelCapabilities,
    ProviderDefaults,
)
from omninexus.providers.secrets import SecretBox, mask_api_key
from omninexus.providers.urls import BaseUrlError, validate_base_url
from omninexus.storage.database import Database

_FORBIDDEN_HEADERS = {"authorization", "x-api-key", "api-key"}


class CustomProviderNotFound(OmniError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CUSTOM_PROVIDER_NOT_FOUND,
            "自定义提供方不存在。",
            status_code=404,
        )


class CustomProviderStore:
    def __init__(self, db: Database, secrets: SecretBox) -> None:
        self.db = db
        self.secrets = secrets

    async def list(self) -> list[CustomProviderRecord]:
        rows = await self.db.fetchall("SELECT * FROM custom_providers ORDER BY created_at ASC")
        models = await self._all_models()
        return [self._row_to_provider(row, models.get(row["id"], [])) for row in rows]

    async def get(self, provider_id: str) -> CustomProviderRecord:
        row = await self.db.fetchone("SELECT * FROM custom_providers WHERE id = ?", (provider_id,))
        if not row:
            raise CustomProviderNotFound()
        models = await self._models_for(provider_id)
        return self._row_to_provider(row, models)

    async def snapshots(self) -> list[CustomProviderSnapshot]:
        rows = await self.db.fetchall("SELECT * FROM custom_providers ORDER BY created_at ASC")
        models = await self._all_models()
        snapshots = []
        for row in rows:
            provider = self._row_to_provider(row, models.get(row["id"], []))
            key = self.secrets.decrypt(row["api_key_ciphertext"], aad=provider.id)
            snapshots.append(CustomProviderSnapshot(provider=provider, api_key=key or None))
        return snapshots

    async def create(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        headers: dict[str, str],
        protocol: str,
        timeout_seconds: int,
        enabled: bool,
        models: list[dict[str, Any]],
        default_model: str | None,
    ) -> CustomProviderRecord:
        name, base_url, headers, timeout_seconds = self._validate_common(
            name, base_url, headers, protocol, timeout_seconds
        )
        normalized_models, default_model = self._validate_models(models, default_model)
        provider_id = str(uuid.uuid4())
        now = iso_now()
        cipher = self.secrets.encrypt(api_key, aad=provider_id) if api_key else ""
        masked = mask_api_key(api_key)
        try:

            def _create(conn: sqlite3.Connection) -> None:
                conn.execute(
                    """INSERT INTO custom_providers
                       (id, name, base_url, api_key_ciphertext, api_key_masked,
                        headers_json, protocol, timeout_seconds, enabled,
                        default_model, created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        provider_id,
                        name,
                        base_url,
                        cipher or None,
                        masked,
                        json.dumps(headers, ensure_ascii=False),
                        protocol,
                        timeout_seconds,
                        1 if enabled else 0,
                        default_model,
                        now,
                        now,
                    ),
                )
                self._insert_models(conn, provider_id, normalized_models, now)

            await self.db.transaction(_create)
        except sqlite3.IntegrityError as exc:
            raise OmniError(
                ErrorCode.CUSTOM_PROVIDER_EXISTS,
                "已存在同名提供方。",
                status_code=409,
            ) from exc
        return await self.get(provider_id)

    async def update(
        self,
        provider_id: str,
        *,
        name: str,
        base_url: str,
        headers: dict[str, str],
        protocol: str,
        timeout_seconds: int,
        enabled: bool,
        models: list[dict[str, Any]],
        default_model: str | None,
        api_key_update: str | None = None,
        update_key: bool = False,
    ) -> CustomProviderRecord:
        existing = await self.get(provider_id)
        name, base_url, headers, timeout_seconds = self._validate_common(
            name, base_url, headers, protocol, timeout_seconds
        )
        normalized_models, default_model = self._validate_models(models, default_model)
        existing_by_name = {model.name: model for model in existing.models}
        base_changed = existing.base_url != base_url
        now = iso_now()
        api_changed = update_key
        cipher = None
        masked = None
        if update_key:
            cipher = self.secrets.encrypt(api_key_update, aad=provider_id) if api_key_update else ""
            masked = mask_api_key(api_key_update)

        def _update(conn: sqlite3.Connection) -> None:
            fields = [
                "name = ?",
                "base_url = ?",
                "headers_json = ?",
                "protocol = ?",
                "timeout_seconds = ?",
                "enabled = ?",
                "default_model = ?",
                "updated_at = ?",
            ]
            params: list[Any] = [
                name,
                base_url,
                json.dumps(headers, ensure_ascii=False),
                protocol,
                timeout_seconds,
                1 if enabled else 0,
                default_model,
                now,
            ]
            if update_key:
                fields.extend(["api_key_ciphertext = ?", "api_key_masked = ?"])
                params.extend([cipher or None, masked or ""])
            params.append(provider_id)
            result = conn.execute(
                f"UPDATE custom_providers SET {', '.join(fields)} WHERE id = ?",
                tuple(params),
            )
            if result.rowcount == 0:
                raise CustomProviderNotFound()
            conn.execute("DELETE FROM custom_models WHERE provider_id = ?", (provider_id,))
            for model in normalized_models:
                previous = existing_by_name.get(model["name"])
                capabilities = ModelCapabilities.model_validate(model["capabilities"])
                if (
                    previous
                    and not base_changed
                    and not api_changed
                    and previous.capabilities == capabilities
                ):
                    model["verification"] = previous.verification
                    model["last_tested_at"] = previous.last_tested_at
                else:
                    model["verification"] = "unverified"
                    model["last_tested_at"] = None
                self._insert_models(conn, provider_id, [model], now)

        try:
            await self.db.transaction(_update)
        except sqlite3.IntegrityError as exc:
            raise OmniError(
                ErrorCode.CUSTOM_PROVIDER_EXISTS,
                "已存在同名提供方。",
                status_code=409,
            ) from exc
        return await self.get(provider_id)

    async def set_enabled(self, provider_id: str, enabled: bool) -> CustomProviderRecord:
        await self.get(provider_id)
        await self.db.execute(
            "UPDATE custom_providers SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, iso_now(), provider_id),
        )
        return await self.get(provider_id)

    async def set_model_verification(
        self, provider_id: str, model_name: str, verification: str
    ) -> None:
        await self.db.execute(
            """UPDATE custom_models SET verification = ?, last_tested_at = ?, updated_at = ?
               WHERE provider_id = ? AND name = ?""",
            (verification, iso_now(), iso_now(), provider_id, model_name),
        )

    async def delete(self, provider_id: str) -> None:
        await self.get(provider_id)
        await self.db.execute("DELETE FROM custom_providers WHERE id = ?", (provider_id,))

    async def usage_count(self, provider_id: str) -> int:
        row = await self.db.fetchone(
            """SELECT COUNT(*) AS n FROM sessions
               WHERE provider_id = ? OR model LIKE ?""",
            (provider_id, f"custom:{provider_id}:%"),
        )
        return int(row["n"]) if row else 0

    async def is_default(self, provider_id: str) -> bool:
        defaults = await self.defaults()
        return bool(
            defaults and defaults.provider_kind == "custom" and defaults.provider_id == provider_id
        )

    async def defaults(self) -> ProviderDefaults | None:
        row = await self.db.fetchone("SELECT value FROM app_settings WHERE key = ?", ("defaults",))
        if not row:
            return None
        try:
            return ProviderDefaults.model_validate(json.loads(row["value"]))
        except (ValueError, TypeError):
            return None

    async def set_defaults(self, defaults: ProviderDefaults) -> None:
        await self.db.execute(
            """INSERT INTO app_settings(key, value, updated_at) VALUES('defaults', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (defaults.model_dump_json(), iso_now()),
        )

    async def export_config(self) -> dict[str, Any]:
        providers = []
        for provider in await self.list():
            providers.append(
                {
                    "name": provider.name,
                    "baseUrl": provider.base_url,
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
                        }
                        for model in provider.models
                    ],
                    "defaultModel": provider.default_model,
                    "hasApiKey": provider.has_api_key,
                }
            )
        return {"schemaVersion": 1, "exportedAt": iso_now(), "providers": providers}

    async def import_config(self, payload: dict[str, Any]) -> list[CustomProviderRecord]:
        providers = payload.get("providers")
        if not isinstance(providers, list):
            raise OmniError(
                ErrorCode.CONFIG_IMPORT_INVALID,
                "导入文件缺少 providers 数组。",
                status_code=422,
            )
        created = []
        existing_names = {provider.name for provider in await self.list()}
        for item in providers[:100]:
            if not isinstance(item, dict):
                continue
            base_name = str(item.get("name") or "imported").strip()[:80] or "imported"
            name = base_name
            index = 2
            while name in existing_names:
                name = f"{base_name} (imported {index})"[:80]
                index += 1
            models = item.get("models") if isinstance(item.get("models"), list) else []
            if not models:
                continue
            try:
                provider = await self.create(
                    name=name,
                    base_url=str(item.get("baseUrl") or ""),
                    api_key=None,
                    headers=item.get("headers") if isinstance(item.get("headers"), dict) else {},
                    protocol=str(item.get("protocol") or "openai-compatible"),
                    timeout_seconds=int(item.get("timeoutSeconds") or 10),
                    enabled=False,
                    models=models,
                    default_model=item.get("defaultModel"),
                )
            except (OmniError, ValueError, BaseUrlError):
                continue
            existing_names.add(name)
            created.append(provider)
        return created

    async def _models_for(self, provider_id: str) -> list[CustomModelRecord]:
        rows = await self.db.fetchall(
            "SELECT * FROM custom_models WHERE provider_id = ? ORDER BY created_at ASC",
            (provider_id,),
        )
        return [self._row_to_model(row) for row in rows]

    async def _all_models(self) -> dict[str, list[CustomModelRecord]]:
        rows = await self.db.fetchall("SELECT * FROM custom_models ORDER BY created_at ASC")
        grouped: dict[str, list[CustomModelRecord]] = {}
        for row in rows:
            grouped.setdefault(row["provider_id"], []).append(self._row_to_model(row))
        return grouped

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> CustomModelRecord:
        try:
            capabilities = ModelCapabilities.model_validate(
                json.loads(row["capabilities_json"] or "{}")
            )
        except (ValueError, TypeError):
            capabilities = ModelCapabilities()
        return CustomModelRecord(
            id=row["id"],
            name=row["name"],
            label=row["label"] or row["name"],
            capabilities=capabilities,
            verification=row["verification"] or "unverified",
            last_tested_at=row["last_tested_at"],
        )

    @classmethod
    def _row_to_provider(
        cls, row: sqlite3.Row, models: list[CustomModelRecord]
    ) -> CustomProviderRecord:
        try:
            headers = json.loads(row["headers_json"] or "{}")
        except (ValueError, TypeError):
            headers = {}
        return CustomProviderRecord(
            id=row["id"],
            name=row["name"],
            base_url=row["base_url"],
            api_key_masked=row["api_key_masked"] or "",
            has_api_key=bool(row["api_key_ciphertext"]),
            headers=headers if isinstance(headers, dict) else {},
            protocol=row["protocol"] or "openai-compatible",
            timeout_seconds=int(row["timeout_seconds"] or 10),
            enabled=bool(row["enabled"]),
            models=models,
            default_model=row["default_model"] or next((m.name for m in models), None),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _validate_common(
        name: str,
        base_url: str,
        headers: dict[str, str],
        protocol: str,
        timeout_seconds: int,
    ) -> tuple[str, str, dict[str, str], int]:
        name = " ".join((name or "").split())[:80]
        if not name:
            raise OmniError(ErrorCode.INVALID_INPUT, "提供方名称不能为空。", status_code=422)
        try:
            base_url = validate_base_url(base_url)
        except BaseUrlError as exc:
            raise OmniError(ErrorCode.INVALID_INPUT, str(exc), status_code=422) from exc
        if protocol != "openai-compatible":
            raise OmniError(
                ErrorCode.UNSUPPORTED_PROTOCOL,
                "当前仅支持 OpenAI 兼容接口。",
                status_code=422,
            )
        if not 1 <= int(timeout_seconds) <= 120:
            raise OmniError(
                ErrorCode.INVALID_INPUT,
                "测试超时必须在 1–120 秒之间。",
                status_code=422,
            )
        clean_headers: dict[str, str] = {}
        for key, value in (headers or {}).items():
            key = str(key).strip()
            if not key or len(key) > 128:
                continue
            if key.lower() in _FORBIDDEN_HEADERS:
                raise OmniError(
                    ErrorCode.INVALID_INPUT,
                    "请求头中不能包含 Authorization / x-api-key，请使用 API Key 字段。",
                    status_code=422,
                )
            clean_headers[key] = str(value)[:2048]
        if len(clean_headers) > 32:
            raise OmniError(ErrorCode.INVALID_INPUT, "自定义请求头最多 32 个。", status_code=422)
        return name, base_url, clean_headers, int(timeout_seconds)

    @staticmethod
    def _validate_models(
        models: list[dict[str, Any]], default_model: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not models:
            raise OmniError(ErrorCode.INVALID_INPUT, "至少需要添加一个模型。", status_code=422)
        if len(models) > 200:
            raise OmniError(ErrorCode.INVALID_INPUT, "单个提供方最多 200 个模型。", status_code=422)
        normalized = []
        seen: set[str] = set()
        for item in models:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or len(name) > 200 or name in seen:
                continue
            seen.add(name)
            label = str(item.get("label") or name).strip()[:200] or name
            raw_caps = item.get("capabilities") or {}
            if isinstance(raw_caps, str):
                try:
                    raw_caps = json.loads(raw_caps)
                except ValueError:
                    raw_caps = {}
            if "inputModalities" in raw_caps:
                raw_caps = {
                    "input_modalities": raw_caps.get("inputModalities"),
                    "context_window": raw_caps.get("contextWindow", 32768),
                    "max_output_tokens": raw_caps.get("maxOutputTokens", 2048),
                    "supports_streaming": raw_caps.get("supportsStreaming", True),
                    "supports_tools": raw_caps.get("supportsTools", False),
                }
            capabilities = ModelCapabilities.model_validate(raw_caps or {})
            normalized.append(
                {
                    "name": name,
                    "label": label,
                    "capabilities": capabilities,
                    "verification": "unverified",
                    "last_tested_at": None,
                }
            )
        if not normalized:
            raise OmniError(ErrorCode.INVALID_INPUT, "模型名称不能为空。", status_code=422)
        names = {item["name"] for item in normalized}
        default_model = (default_model or "").strip() or normalized[0]["name"]
        if default_model not in names:
            raise OmniError(
                ErrorCode.INVALID_INPUT,
                "默认模型必须属于该提供方的模型列表。",
                status_code=422,
            )
        return normalized, default_model

    @staticmethod
    def _insert_models(
        conn: sqlite3.Connection,
        provider_id: str,
        models: list[dict[str, Any]],
        now: str,
    ) -> None:
        for model in models:
            conn.execute(
                """INSERT INTO custom_models
                   (id, provider_id, name, label, capabilities_json, verification,
                    last_tested_at, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    provider_id,
                    model["name"],
                    model["label"],
                    model["capabilities"].model_dump_json(),
                    model.get("verification") or "unverified",
                    model.get("last_tested_at"),
                    now,
                    now,
                ),
            )
