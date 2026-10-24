"""Privacy and secret-handling regression tests."""

from __future__ import annotations

import io
import logging

import httpx
import pytest

from omninexus.core.models import InboundEvent
from omninexus.logging import RedactionFilter, get_logger, redact, register_secret
from tests.conftest import app_client, base_config, make_transport


def test_redactor_removes_common_key_formats() -> None:
    register_secret("alpha-test-secret")
    text = (
        "Authorization: Bearer alpha-test-secret and sk-abcdefghijk123456 "
        "and api_key=supersecretvalue AIzaabcdefghijklmnopqrstuvwxyz"
    )
    cleaned = redact(text)
    assert "alpha-test-secret" not in cleaned
    assert "sk-abcdefghijk123456" not in cleaned
    assert "supersecretvalue" not in cleaned
    assert "AIzaabcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "[REDACTED]" in cleaned

    record = logging.LogRecord(
        "omninexus.test", logging.INFO, __file__, 1, "token=%s", ("alpha-test-secret",), None
    )
    RedactionFilter().filter(record)
    assert "alpha-test-secret" not in record.getMessage()


def test_part_metadata_drops_credentials() -> None:
    event = InboundEvent(
        event_id="60606060-6060-4060-8060-606060606060",
        parts=[
            {
                "modality": "text",
                "content": "hello",
                "metadata": {
                    "api_key": "must-not-be-stored",
                    "authorization": "Bearer nope",
                    "filename": "note.txt",
                },
            }
        ],
    )
    part = event.normalized_parts()[0]
    assert "api_key" not in part.metadata
    assert "authorization" not in part.metadata
    assert part.metadata["filename"] == "note.txt"


@pytest.mark.asyncio
async def test_api_never_returns_secret_and_upstream_body_is_not_echoed(tmp_path) -> None:
    config = base_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"message": "bad key alpha-test-secret", "key": "alpha-test-secret"}},
        )

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        providers = await client.get("/v1/providers")
        assert "alpha-test-secret" not in providers.text
        assert "ALPHA_KEY" in providers.text

        settings = await client.get("/v1/settings")
        assert "alpha-test-secret" not in settings.text

        response = await client.post(
            "/v1/events",
            json={
                "event_id": "70707070-7070-4070-8070-707070707070",
                "parts": [{"modality": "text", "content": "private conversation text"}],
            },
        )
        assert response.status_code == 503
        assert "alpha-test-secret" not in response.text
        assert "private conversation text" not in response.text
        assert "bad key" not in response.text


@pytest.mark.asyncio
async def test_runtime_logs_do_not_contain_conversation_or_secret(tmp_path) -> None:
    config = base_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"message": "alpha-test-secret and PRIVATE_LOG_MARKER"}},
        )

    buffer = io.StringIO()
    handler_obj = logging.StreamHandler(buffer)
    handler_obj.addFilter(RedactionFilter())
    logger = get_logger()
    logger.addHandler(handler_obj)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        async with app_client(config, transport=make_transport(handler)) as (client, _):
            response = await client.post(
                "/v1/events",
                json={
                    "event_id": "90909090-9090-4090-8090-909090909090",
                    "parts": [{"modality": "text", "content": "PRIVATE_LOG_MARKER"}],
                },
            )
            assert response.status_code == 503
    finally:
        logger.removeHandler(handler_obj)
        logger.setLevel(previous_level)
    logs = buffer.getvalue()
    assert "PRIVATE_LOG_MARKER" not in logs
    assert "alpha-test-secret" not in logs


@pytest.mark.asyncio
async def test_input_metadata_is_not_archived_verbatim(tmp_path) -> None:
    config = base_config(tmp_path)
    config.gateway.default_model = "echo:echo"

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(500)

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "80808080-8080-4080-8080-808080808080",
                "parts": [{"modality": "text", "content": "hello"}],
                "metadata": {"api_key": "should-not-be-saved", "locale": "zh-CN"},
            },
        )
        assert response.status_code == 200
        detail = await client.get(f"/v1/sessions/{response.json()['session_id']}")
        stored = detail.text
        assert "should-not-be-saved" not in stored
