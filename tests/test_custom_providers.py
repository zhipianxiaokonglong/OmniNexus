"""Custom provider lifecycle, testing, strict URLs, defaults and migration."""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest

from omninexus.logging import RedactionFilter, get_logger
from tests.conftest import Recorder, app_client, base_config, make_transport, sse_response

KEY = "sk-test-abcd1234"


def capabilities(*modalities: str) -> dict:
    return {
        "inputModalities": list(modalities) or ["text"],
        "contextWindow": 8192,
        "maxOutputTokens": 512,
        "supportsStreaming": True,
    }


class CustomBackend:
    def __init__(self) -> None:
        self.recorder = Recorder()
        self.require_auth = False
        self.models = ["my-model", "vision-model"]
        self.fail = ""
        self.vision_seen = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.recorder.requests.append(request)
        if request.url.host in {"slow.test", "down.test"}:
            if request.url.host == "slow.test":
                raise httpx.ReadTimeout("slow", request=request)
            raise httpx.ConnectError("down", request=request)
        path = request.url.path
        if request.method == "GET" and path.rstrip("/") in {"", "/v1"}:
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET" and path in {"/models", "/v1/models"}:
            if self.require_auth and request.headers.get("authorization") != f"Bearer {KEY}":
                return httpx.Response(401, json={"error": "auth"})
            return httpx.Response(200, json={"data": [{"id": name} for name in self.models]})
        if request.method == "POST" and path in {"/chat/completions", "/v1/chat/completions"}:
            body = json.loads(request.read())
            if body.get("model") not in self.models:
                return httpx.Response(404, json={"error": "model not found"})
            content = body["messages"][-1]["content"]
            if isinstance(content, list) and any(
                item.get("type") == "image_url" for item in content
            ):
                self.vision_seen = True
            if body.get("stream"):
                return sse_response("custom streaming answer")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]},
            )
        return httpx.Response(404)


async def create_provider(
    client: httpx.AsyncClient,
    *,
    name: str = "My Relay",
    base_url: str = "http://custom.test/v1",
    api_key: str | None = KEY,
    models: list[dict] | None = None,
    default_model: str = "my-model",
    enabled: bool = True,
) -> dict:
    response = await client.post(
        "/v1/custom-providers",
        json={
            "name": name,
            "baseUrl": base_url,
            "apiKey": api_key,
            "headers": {"X-Test": "yes"},
            "protocol": "openai-compatible",
            "timeoutSeconds": 10,
            "enabled": enabled,
            "models": models
            or [
                {"name": "my-model", "capabilities": capabilities("text")},
                {"name": "vision-model", "capabilities": capabilities("text", "image")},
            ],
            "defaultModel": default_model,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["provider"]


@pytest.mark.asyncio
async def test_custom_provider_crud_masks_key_and_encrypts(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, app):
        provider = await create_provider(client)
        assert provider["apiKeyMasked"] == KEY[:3] + "****" + KEY[-4:]
        assert provider["hasApiKey"] is True
        assert "apiKey" not in provider
        assert KEY not in json.dumps(provider)

        row = await app.state.db.fetchone(
            "SELECT api_key_ciphertext, api_key_masked FROM custom_providers WHERE id = ?",
            (provider["id"],),
        )
        assert row["api_key_ciphertext"].startswith("v1:")
        assert KEY not in row["api_key_ciphertext"]
        assert row["api_key_masked"] == provider["apiKeyMasked"]

        listed = await client.get("/v1/custom-providers")
        assert listed.status_code == 200
        assert KEY not in listed.text
        assert listed.json()["providers"][0]["apiKeyMasked"].endswith("1234")

        # Omitted key on update keeps the existing encrypted key.
        update = await client.put(
            f"/v1/custom-providers/{provider['id']}",
            json={
                "name": "Renamed",
                "baseUrl": provider["baseUrl"],
                "headers": {},
                "protocol": "openai-compatible",
                "timeoutSeconds": 12,
                "enabled": True,
                "models": provider["models"],
                "defaultModel": "my-model",
            },
        )
        assert update.status_code == 200, update.text
        assert update.json()["provider"]["hasApiKey"] is True
        assert update.json()["provider"]["timeoutSeconds"] == 12


@pytest.mark.asyncio
async def test_custom_provider_test_levels(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        success = await client.post(
            "/v1/custom-providers/test",
            json={
                "baseUrl": "http://custom.test/v1",
                "apiKey": KEY,
                "model": "my-model",
                "timeoutSeconds": 10,
            },
        )
        assert success.status_code == 200
        result = success.json()["result"]
        assert result["status"] == "success"
        assert [level["status"] for level in result["levels"]] == ["ok", "ok", "ok"]

        missing = await client.post(
            "/v1/custom-providers/test",
            json={
                "baseUrl": "http://custom.test/v1",
                "apiKey": KEY,
                "model": "missing-model",
                "timeoutSeconds": 10,
            },
        )
        assert missing.json()["result"]["status"] == "model_not_found"

        backend.require_auth = True
        auth = await client.post(
            "/v1/custom-providers/test",
            json={
                "baseUrl": "http://custom.test/v1",
                "apiKey": "wrong-key",
                "model": "my-model",
                "timeoutSeconds": 10,
            },
        )
        assert auth.json()["result"]["status"] == "auth_failed"
        assert "wrong-key" not in auth.text
        backend.require_auth = False

        timeout = await client.post(
            "/v1/custom-providers/test",
            json={
                "baseUrl": "http://slow.test/v1",
                "apiKey": KEY,
                "model": "my-model",
                "timeoutSeconds": 1,
            },
        )
        assert timeout.json()["result"]["status"] == "timeout"

        unreachable = await client.post(
            "/v1/custom-providers/test",
            json={
                "baseUrl": "http://down.test/v1",
                "apiKey": KEY,
                "model": "my-model",
                "timeoutSeconds": 1,
            },
        )
        assert unreachable.json()["result"]["status"] == "unreachable"

        unverified = await client.post(
            "/v1/custom-providers/test",
            json={
                "baseUrl": "http://custom.test/v1",
                "apiKey": KEY,
                "timeoutSeconds": 10,
            },
        )
        assert unverified.json()["result"]["status"] == "unverified"


@pytest.mark.asyncio
async def test_custom_provider_discovery_and_multimodal_probe(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        discovery = await client.post(
            "/v1/custom-providers/discover-models",
            json={"baseUrl": "http://custom.test/v1", "apiKey": KEY, "timeoutSeconds": 10},
        )
        assert discovery.status_code == 200
        assert discovery.json()["models"] == ["my-model", "vision-model"]

        probe = await client.post(
            "/v1/custom-providers/test",
            json={
                "baseUrl": "http://custom.test/v1",
                "apiKey": KEY,
                "model": "vision-model",
                "probeMultimodal": True,
                "timeoutSeconds": 10,
            },
        )
        assert probe.json()["result"]["multimodal"]["status"] == "verified"
        assert backend.vision_seen is True


@pytest.mark.asyncio
async def test_custom_chat_stream_and_strict_base_url(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        provider = await create_provider(client, base_url="http://custom.test")
        await client.put(
            "/v1/provider-defaults",
            json={
                "providerKind": "custom",
                "providerId": provider["id"],
                "model": "my-model",
            },
        )
        defaults = await client.get("/v1/provider-defaults")
        assert defaults.json()["defaults"]["providerKind"] == "custom"

        created = await client.post("/v1/sessions", json={})
        assert created.status_code == 201
        session = created.json()["session"]
        assert session["provider_kind"] == "custom"
        assert session["provider_id"] == provider["id"]

        response = await client.post(
            "/v1/events",
            json={
                "event_id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
                "session_id": session["id"],
                "parts": [{"modality": "text", "content": "hello"}],
            },
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200
        assert "custom streaming answer" in response.text
        chat_paths = [
            request.url.path
            for request in backend.recorder.requests
            if request.method == "POST" and request.url.path.endswith("/chat/completions")
        ]
        assert chat_paths
        assert all(path == "/chat/completions" for path in chat_paths)


@pytest.mark.asyncio
async def test_custom_capability_routes_within_provider(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        provider = await create_provider(client)
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb",
                "parts": [
                    {"modality": "text", "content": "describe"},
                    {
                        "modality": "image",
                        "content": "data:image/png;base64,AAAA",
                        "metadata": {"alt": "a dot"},
                    },
                ],
                "metadata": {
                    "providerKind": "custom",
                    "providerId": provider["id"],
                    "model": "my-model",
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["content"] == "custom streaming answer"
        chat = [
            json.loads(request.read())
            for request in backend.recorder.requests
            if request.method == "POST" and request.url.path.endswith("/chat/completions")
        ][-1]
        assert chat["model"] == "vision-model"
        assert any(item.get("type") == "image_url" for item in chat["messages"][-1]["content"])


@pytest.mark.asyncio
async def test_model_limit_is_readable_at_boundary(tmp_path) -> None:
    """Issue #1: 200 is accepted, 201 is rejected with an actionable message."""
    config = base_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    def models(count: int) -> list[dict]:
        return [
            {"name": f"model-{index}", "capabilities": capabilities("text")}
            for index in range(count)
        ]

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        accepted = await client.post(
            "/v1/custom-providers",
            json={
                "name": "at-limit",
                "baseUrl": "http://custom.test/v1",
                "models": models(200),
                "defaultModel": "model-0",
            },
        )
        assert accepted.status_code == 201, accepted.text

        rejected = await client.post(
            "/v1/custom-providers",
            json={
                "name": "over-limit",
                "baseUrl": "http://custom.test/v1",
                "models": models(201),
                "defaultModel": "model-0",
            },
        )
        assert rejected.status_code == 422
        payload = rejected.json()["error"]
        assert payload["code"] == "INVALID_INPUT"
        assert "最多 200" in payload["message"]
        assert "Request validation failed" not in payload["message"]
        assert payload["details"]["fields"][0]["field"] == "models"

        provider = accepted.json()["provider"]
        update_rejected = await client.put(
            f"/v1/custom-providers/{provider['id']}",
            json={
                "name": provider["name"],
                "baseUrl": provider["baseUrl"],
                "headers": {},
                "protocol": "openai-compatible",
                "timeoutSeconds": 10,
                "enabled": True,
                "models": models(300),
                "defaultModel": "model-0",
            },
        )
        assert update_rejected.status_code == 422
        assert "最多 200" in update_rejected.json()["error"]["message"]


@pytest.mark.asyncio
async def test_new_session_uses_provider_defaults_not_gateway_default(tmp_path) -> None:
    """Dounai audit 2026-11-05: settings defaults must drive new sessions."""
    config = base_config(tmp_path)
    config.gateway.default_model = "openai:gpt-4o-mini"
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        provider = await create_provider(client)
        await client.put(
            "/v1/provider-defaults",
            json={
                "providerKind": "custom",
                "providerId": provider["id"],
                "model": "my-model",
            },
        )
        stated = (await client.get("/v1/provider-defaults")).json()["defaults"]

        response = await client.post(
            "/v1/events",
            json={
                "event_id": "dddddddd-3333-4333-8333-dddddddddddd",
                "parts": [{"modality": "text", "content": "use my default"}],
            },
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200, response.text
        session_id = response.headers["x-session-id"]
        detail = (await client.get(f"/v1/sessions/{session_id}")).json()["session"]

        assert (detail["provider_kind"], detail["provider_id"], detail["model_name"]) == (
            stated["providerKind"],
            stated["providerId"],
            stated["model"],
        )
        assert detail["model"] == f"custom:{provider['id']}:my-model"
        assert detail["model"] != config.gateway.default_model


@pytest.mark.asyncio
async def test_delete_default_in_use_and_session_migration(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        provider = await create_provider(client)
        await client.put(
            "/v1/provider-defaults",
            json={
                "providerKind": "custom",
                "providerId": provider["id"],
                "model": "my-model",
            },
        )
        session = await client.post("/v1/sessions", json={})
        session_id = session.json()["session"]["id"]

        blocked = await client.delete(f"/v1/custom-providers/{provider['id']}")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "DEFAULT_PROVIDER_REQUIRED"

        await client.put(
            "/v1/provider-defaults",
            json={"providerKind": "builtin", "providerId": "alpha", "model": "one"},
        )
        in_use = await client.delete(f"/v1/custom-providers/{provider['id']}")
        assert in_use.status_code == 409
        assert in_use.json()["error"]["code"] == "PROVIDER_IN_USE"

        confirmed = await client.delete(f"/v1/custom-providers/{provider['id']}?confirm=true")
        assert confirmed.status_code == 204

        detail = await client.get(f"/v1/sessions/{session_id}")
        assert detail.json()["providerAvailable"] is False
        candidates = await client.get("/v1/provider-migration/candidates")
        assert any(item["id"] == session_id for item in candidates.json()["sessions"])

        applied = await client.post(
            "/v1/provider-migration/apply",
            json={
                "sessionIds": [session_id],
                "targetProviderKind": "builtin",
                "targetProviderId": "alpha",
                "targetModel": "one",
            },
        )
        assert applied.status_code == 200
        migrated = await client.get(f"/v1/sessions/{session_id}")
        assert migrated.json()["session"]["model"] == "alpha:one"
        assert migrated.json()["providerAvailable"] is True


@pytest.mark.asyncio
async def test_export_import_never_contains_key(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        await create_provider(client, name="Export Me")
        exported = await client.get("/v1/provider-config/export")
        assert exported.status_code == 200
        text = exported.text
        assert KEY not in text
        assert "apiKey" not in text
        payload = exported.json()
        assert payload["providers"][0]["hasApiKey"] is True

        imported = await client.post("/v1/provider-config/import", json=payload)
        assert imported.status_code == 200
        assert imported.json()["imported"] == 1
        providers = (await client.get("/v1/custom-providers")).json()["providers"]
        imported_provider = next(p for p in providers if p["name"] != "Export Me")
        assert imported_provider["hasApiKey"] is False
        assert imported_provider["enabled"] is False


@pytest.mark.asyncio
async def test_disable_default_and_session_becomes_unavailable(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        provider = await create_provider(client)
        await client.put(
            "/v1/provider-defaults",
            json={
                "providerKind": "custom",
                "providerId": provider["id"],
                "model": "my-model",
            },
        )
        session = await client.post("/v1/sessions", json={})
        session_id = session.json()["session"]["id"]

        blocked = await client.patch(
            f"/v1/custom-providers/{provider['id']}/enabled",
            json={"enabled": False},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "DEFAULT_PROVIDER_REQUIRED"

        await client.put(
            "/v1/provider-defaults",
            json={"providerKind": "builtin", "providerId": "alpha", "model": "one"},
        )
        disabled = await client.patch(
            f"/v1/custom-providers/{provider['id']}/enabled",
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        detail = await client.get(f"/v1/sessions/{session_id}")
        assert detail.json()["providerAvailable"] is False
        assert detail.json()["providerUnavailableReason"] == "provider_disabled"

        chat = await client.post(
            "/v1/events",
            json={
                "event_id": "cccccccc-2222-4222-8222-cccccccccccc",
                "session_id": session_id,
                "parts": [{"modality": "text", "content": "still there?"}],
            },
        )
        assert chat.status_code == 409
        assert chat.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_saved_test_updates_verification_and_clear_key(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        provider = await create_provider(client)
        tested = await client.post(
            f"/v1/custom-providers/{provider['id']}/test",
            json={"model": "my-model", "probeMultimodal": False},
        )
        assert tested.status_code == 200
        assert tested.json()["result"]["status"] == "success"
        listed = await client.get(f"/v1/custom-providers/{provider['id']}")
        assert listed.json()["provider"]["models"][0]["verification"] == "text_verified"

        cleared = await client.put(
            f"/v1/custom-providers/{provider['id']}",
            json={
                "name": provider["name"],
                "baseUrl": provider["baseUrl"],
                "clearApiKey": True,
                "headers": {},
                "protocol": "openai-compatible",
                "timeoutSeconds": 10,
                "enabled": True,
                "models": provider["models"],
                "defaultModel": "my-model",
            },
        )
        assert cleared.status_code == 200
        assert cleared.json()["provider"]["hasApiKey"] is False
        assert cleared.json()["provider"]["apiKeyMasked"] == ""


@pytest.mark.asyncio
async def test_provider_validation_errors(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()

    async with app_client(config, transport=make_transport(backend)) as (client, _):
        provider = await create_provider(client)
        duplicate = await client.post(
            "/v1/custom-providers",
            json={
                "name": provider["name"],
                "baseUrl": "http://custom.test/v1",
                "models": [{"name": "x", "capabilities": capabilities("text")}],
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "CUSTOM_PROVIDER_EXISTS"

        bad_protocol = await client.post(
            "/v1/custom-providers",
            json={
                "name": "bad",
                "baseUrl": "http://custom.test/v1",
                "protocol": "anthropic",
                "models": [{"name": "x", "capabilities": capabilities("text")}],
            },
        )
        assert bad_protocol.status_code == 422

        bad_default = await client.post(
            "/v1/custom-providers",
            json={
                "name": "bad-default",
                "baseUrl": "http://custom.test/v1",
                "models": [{"name": "x", "capabilities": capabilities("text")}],
                "defaultModel": "not-in-list",
            },
        )
        assert bad_default.status_code == 422


@pytest.mark.asyncio
async def test_custom_key_never_appears_in_logs(tmp_path) -> None:
    config = base_config(tmp_path)
    backend = CustomBackend()
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.addFilter(RedactionFilter())
    logger = get_logger()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        async with app_client(config, transport=make_transport(backend)) as (client, _):
            provider = await create_provider(client)
            await client.post(
                f"/v1/custom-providers/{provider['id']}/test",
                json={"model": "my-model"},
            )
            await client.post(
                "/v1/custom-providers/test",
                json={
                    "baseUrl": "http://custom.test/v1",
                    "apiKey": KEY,
                    "model": "my-model",
                },
            )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    logs = buffer.getvalue()
    assert KEY not in logs
    assert "sk-test-abcd" not in logs


def test_strict_url_join_never_adds_v1() -> None:
    from omninexus.providers.urls import (
        BaseUrlError,
        chat_completions_url,
        models_url,
        validate_base_url,
    )

    assert models_url("https://api.example.com") == "https://api.example.com/models"
    assert chat_completions_url("https://api.example.com/v1/") == (
        "https://api.example.com/v1/chat/completions"
    )
    assert validate_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    with pytest.raises(BaseUrlError):
        validate_base_url("https://api.example.com/v1/models")
    with pytest.raises(BaseUrlError):
        validate_base_url("ftp://api.example.com/v1")
