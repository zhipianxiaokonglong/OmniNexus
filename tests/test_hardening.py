"""Hardening tests: request size limits, bind guard, error-envelope parity."""

from __future__ import annotations

import httpx
import pytest

from omninexus.config import ConfigError
from tests.conftest import app_client, base_config


@pytest.mark.asyncio
async def test_body_limit_applies_without_content_length(tmp_path) -> None:
    """A chunked/streamed body must not bypass server.max_body_bytes."""
    config = base_config(tmp_path)
    config.server.max_body_bytes = 256

    async def chunks():
        for _ in range(8):
            yield b"x" * 128

    async with app_client(config) as (client, _):
        request = client.build_request(
            "POST",
            "/v1/events",
            content=chunks(),
            headers={"Content-Type": "application/json"},
        )
        assert "content-length" not in request.headers
        response = await client.send(request)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_body_limit_applies_with_declared_length(tmp_path) -> None:
    config = base_config(tmp_path)
    config.server.max_body_bytes = 64
    async with app_client(config) as (client, _):
        response = await client.post(
            "/v1/events",
            content=b"y" * 256,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413


@pytest.mark.asyncio
async def test_normal_request_is_unaffected_by_limit(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        response = await client.post(
            "/v1/events",
            json={"event_id": "abababab-9999-4999-8999-abababababab", "parts": []},
        )
        assert response.status_code == 422


def test_non_loopback_bind_is_refused_without_flag(monkeypatch, tmp_path) -> None:
    from omninexus.__main__ import main
    from omninexus.config import load_config

    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "data"))
    config = load_config()
    config.server.host = "0.0.0.0"
    config.server.allow_insecure_bind = False

    def fake_load_config(*args, **kwargs):
        return config

    monkeypatch.setattr("omninexus.config.load_config", fake_load_config)
    assert main([]) == 2


def test_loopback_bind_is_allowed(monkeypatch, tmp_path) -> None:
    from omninexus.__main__ import main
    from omninexus.config import load_config

    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "data"))
    config = load_config()
    config.server.host = "127.0.0.1"

    def fake_load_config(*args, **kwargs):
        return config

    calls: list[dict] = []
    monkeypatch.setattr("omninexus.config.load_config", fake_load_config)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    assert main([]) == 0
    assert calls and calls[0]["host"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_app_lifespan_refuses_insecure_bind(tmp_path) -> None:
    config = base_config(tmp_path)
    config.server.host = "0.0.0.0"
    config.server.allow_insecure_bind = False
    with pytest.raises(ConfigError):
        async with app_client(config):
            pass


@pytest.mark.asyncio
async def test_error_envelope_matches_contract_everywhere(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        for bad in (
            {"event_id": "not-a-uuid", "parts": []},
            {"event_id": "abababab-8888-4888-8888-abababababab"},
        ):
            response = await client.post("/v1/events", json=bad)
            assert response.status_code >= 400
            error = response.json()["error"]
            assert {"code", "message", "retryable"} <= set(error)
            assert error["request_id"] == response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_transport_error_keeps_retryable_in_json_mode(tmp_path) -> None:
    """run_json rebuilds errors from SSE frames; retryable must survive."""
    config = base_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    from tests.conftest import make_transport

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "abababab-7777-4777-8777-abababababab",
                "parts": [{"modality": "text", "content": "hi"}],
            },
        )
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "ALL_PROVIDERS_FAILED"
        assert error["retryable"] is False
