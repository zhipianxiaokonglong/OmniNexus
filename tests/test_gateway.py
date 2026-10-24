"""Gateway tests: retry, fallback, context continuity, model switch, multimodal."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import Recorder, app_client, base_config, make_transport, sse_response

DEFAULT_EVENT_ID = "11111111-1111-4111-8111-111111111111"


def _event(
    text: str,
    session_id: str | None = None,
    event_id: str = DEFAULT_EVENT_ID,
):
    payload = {
        "event_id": event_id,
        "session_id": session_id,
        "parts": [{"modality": "text", "content": text}],
    }
    return payload


@pytest.mark.asyncio
async def test_fallback_preserves_context(tmp_path) -> None:
    config = base_config(tmp_path)
    recorder = Recorder()
    alpha_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal alpha_calls
        recorder.requests.append(request)
        if request.url.host == "alpha.test":
            alpha_calls += 1
            if alpha_calls == 1:
                return sse_response("first answer from alpha")
            return httpx.Response(500, json={"error": {"message": "upstream down"}})
        if request.url.host == "beta.test":
            return sse_response("fallback answer from beta")
        return httpx.Response(404)

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        first = await client.post("/v1/events", json=_event("remember this topic"))
        assert first.status_code == 200
        assert "first answer" in first.json()["content"]
        session_id = first.json()["session_id"]

        second = await client.post(
            "/v1/events",
            json=_event("what did we discuss?", session_id, "22222222-2222-4222-8222-222222222222"),
        )
        assert second.status_code == 200
        assert second.json()["content"] == "fallback answer from beta"
        assert second.json()["tags"]["intent"] == "reply"

        beta_body = recorder.with_host("beta.test")[-1]
        import json as jsonlib

        body = jsonlib.loads(beta_body.read())
        flattened = jsonlib.dumps(body["messages"], ensure_ascii=False)
        assert "first answer from alpha" in flattened
        assert "remember this topic" in flattened
        assert "what did we discuss?" in flattened


@pytest.mark.asyncio
async def test_retry_same_provider_before_fallback(tmp_path) -> None:
    config = base_config(tmp_path)
    config.gateway.max_retries = 1
    recorder = Recorder()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        recorder.requests.append(request)
        if request.url.host == "alpha.test":
            attempts += 1
            if attempts == 1:
                return httpx.Response(500, json={"error": {"message": "transient"}})
            return sse_response("retry succeeded")
        return sse_response("should not be used")

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post("/v1/events", json=_event("retry please"))
        assert response.status_code == 200
        assert response.json()["content"] == "retry succeeded"
        assert attempts == 2
        assert not recorder.with_host("beta.test")


@pytest.mark.asyncio
async def test_model_switch_uses_same_archive_context(tmp_path) -> None:
    config = base_config(tmp_path)
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "alpha.test":
            return sse_response("alpha first answer")
        return sse_response("beta second answer")

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        first = await client.post("/v1/events", json=_event("alpha turn"))
        session_id = first.json()["session_id"]
        switched = await client.post(f"/v1/sessions/{session_id}/model", json={"model": "beta:one"})
        assert switched.status_code == 200
        second = await client.post(
            "/v1/events",
            json=_event("beta turn", session_id, "22222222-2222-4222-8222-222222222222"),
        )
        assert second.status_code == 200
        assert second.json()["content"] == "beta second answer"
        import json as jsonlib

        body = jsonlib.loads(recorder.with_host("beta.test")[-1].read())
        flattened = jsonlib.dumps(body["messages"], ensure_ascii=False)
        assert "alpha first answer" in flattened
        assert "alpha turn" in flattened


@pytest.mark.asyncio
async def test_capability_route_to_vision_model(tmp_path) -> None:
    config = base_config(tmp_path)
    config.models[0].fallbacks = ["alpha:vision"]
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "alpha.test":
            return sse_response("vision answer")
        return httpx.Response(500)

    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    payload = {
        "event_id": "33333333-3333-4333-8333-333333333333",
        "parts": [
            {"modality": "text", "content": "what is this?"},
            {
                "modality": "image",
                "content": image,
                "metadata": {"mime_type": "image/png", "alt": "a dot"},
            },
        ],
    }
    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post("/v1/events", json=payload)
        assert response.status_code == 200
        assert response.json()["content"] == "vision answer"

    import json as jsonlib

    body = jsonlib.loads(recorder.with_host("alpha.test")[-1].read())
    assert body["model"] == "vision"
    content = body["messages"][-1]["content"]
    assert isinstance(content, list)
    assert any(item.get("type") == "image_url" for item in content)


@pytest.mark.asyncio
async def test_capability_routes_to_configured_vision_outside_chain(tmp_path) -> None:
    config = base_config(tmp_path)
    config.models[0].fallbacks = []
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "vision.test":
            return sse_response("outside-chain vision")
        return httpx.Response(500)

    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    payload = {
        "event_id": "34343434-3434-4434-8434-343434343434",
        "parts": [
            {"modality": "text", "content": "what is this?"},
            {"modality": "image", "content": image},
        ],
    }
    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post("/v1/events", json=payload)
        assert response.status_code == 200
        assert response.json()["content"] == "outside-chain vision"

    import json as jsonlib

    body = jsonlib.loads(recorder.with_host("vision.test")[-1].read())
    assert body["model"] == "one"
    assert any(item.get("type") == "image_url" for item in body["messages"][-1]["content"])


@pytest.mark.asyncio
async def test_capability_degrade_to_text_when_no_vision(tmp_path) -> None:
    config = base_config(tmp_path)
    config.models = [model for model in config.models if "vision" not in model.id]
    config.models[0].fallbacks = []
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return sse_response("text-only answer")

    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    payload = {
        "event_id": "44444444-4444-4444-8444-444444444444",
        "parts": [
            {"modality": "text", "content": "look"},
            {
                "modality": "image",
                "content": image,
                "metadata": {"alt": "a tiny dot", "mime_type": "image/png"},
            },
        ],
    }
    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post("/v1/events", json=payload)
        assert response.status_code == 200
        assert response.json()["content"] == "text-only answer"

    import json as jsonlib

    body = jsonlib.loads(recorder.with_host("alpha.test")[-1].read())
    flattened = jsonlib.dumps(body["messages"], ensure_ascii=False)
    assert "image_url" not in flattened
    assert "tiny dot" in flattened


@pytest.mark.asyncio
async def test_vision_primary_falls_back_to_text_safely(tmp_path) -> None:
    config = base_config(tmp_path)
    config.gateway.default_model = "vision:one"
    config.models[-2].fallbacks = ["beta:one"]  # vision:one
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "vision.test":
            return httpx.Response(503, json={"error": {"message": "down"}})
        return sse_response("text fallback")

    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    payload = {
        "event_id": "55555555-5555-4555-8555-555555555555",
        "parts": [
            {"modality": "text", "content": "look"},
            {"modality": "image", "content": image},
        ],
    }
    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post("/v1/events", json=payload)
        assert response.status_code == 200
        assert response.json()["content"] == "text fallback"

    import json as jsonlib

    body = jsonlib.loads(recorder.with_host("beta.test")[-1].read())
    flattened = jsonlib.dumps(body["messages"], ensure_ascii=False)
    assert "image_url" not in flattened
    assert "Image omitted" in flattened


@pytest.mark.asyncio
async def test_all_providers_failed_returns_structured_error(tmp_path) -> None:
    config = base_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "down"}})

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post("/v1/events", json=_event("fail"))
        assert response.status_code == 503
        payload = response.json()
        assert payload["error"]["code"] == "ALL_PROVIDERS_FAILED"
        assert len(payload["error"]["details"]["attempts"]) >= 2
        assert "down" not in response.text


@pytest.mark.asyncio
async def test_offline_default_model_reroutes_to_configured_echo(tmp_path, monkeypatch) -> None:
    config = base_config(tmp_path)
    config.gateway.default_model = "alpha:one"
    config.providers["echo"].enabled = True
    for name in ("ALPHA_KEY", "BETA_KEY", "VISION_KEY"):
        monkeypatch.delenv(name, raising=False)

    async with app_client(config) as (client, _):
        response = await client.post("/v1/events", json=_event("offline default"))
        assert response.status_code == 200
        assert "Offline diagnostic echo" in response.json()["content"]


@pytest.mark.asyncio
async def test_provider_timeout_falls_back(tmp_path) -> None:
    config = base_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "alpha.test":
            raise httpx.ReadTimeout("slow upstream", request=request)
        return sse_response("beta after timeout")

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post("/v1/events", json=_event("timeout test"))
        assert response.status_code == 200
        assert response.json()["content"] == "beta after timeout"


@pytest.mark.asyncio
async def test_local_rate_limit_falls_back(tmp_path) -> None:
    config = base_config(tmp_path)
    config.gateway.rate_limit_rpm = 1
    config.gateway.rate_limit_max_wait_s = 0
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "alpha.test":
            return sse_response("alpha first")
        return sse_response("beta after rate limit")

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        first = await client.post("/v1/events", json=_event("first"))
        assert first.status_code == 200
        second = await client.post(
            "/v1/events",
            json=_event("second", event_id="99999999-9999-4999-8999-999999999999"),
        )
        assert second.status_code == 200
        assert second.json()["content"] == "beta after rate limit"
