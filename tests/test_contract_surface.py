"""Cross-repository contract surface: versions, labels, and error envelope."""

from __future__ import annotations

import pytest

from tests.conftest import app_client, base_config


@pytest.mark.asyncio
async def test_contract_endpoint_publishes_machine_readable_versions(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        response = await client.get("/v1/contract")
        assert response.status_code == 200
        payload = response.json()

        assert payload["service"] == "omninexus"
        assert payload["service_version"]
        contracts = payload["contracts"]

        emotion = contracts["emotion"]
        assert emotion["version"] == "2"
        assert len(emotion["labels"]) == 12
        assert emotion["legacy_aliases"] == {
            "fear": "sadness",
            "confusion": "frustration",
            "gratitude": "joy",
        }
        assert "comfort" in emotion["labels"]
        assert "calm" in emotion["labels"]

        event = contracts["event"]
        assert event["version"] == "1.0"
        assert set(event["tag_fields"]) == {"emotion", "type", "intent"}

        memory = contracts["memory"]
        assert memory["status"] == "reserved"
        assert memory["default_enabled"] is False

        error = contracts["error"]
        assert "retryable" in error["fields"]
        assert error["request_id_header"] == "X-Request-ID"

        assert payload["compatibility"]["policy"] == "major_reject_minor_degrade"


@pytest.mark.asyncio
async def test_healthz_exposes_service_and_contract_versions(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        payload = (await client.get("/v1/healthz")).json()
        assert payload["status"] == "ok"
        # Module version and contract version must be distinguishable.
        assert payload["service_version"]
        assert payload["contract_version"] == "1.0"
        assert payload["tag_contract"] == "2"


@pytest.mark.asyncio
async def test_error_envelope_has_retryable_and_request_id(tmp_path) -> None:
    """D1: downstream consumers can classify retries without guessing codes."""
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "abababab-1111-4111-8111-abababababab",
                "parts": [],
            },
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["retryable"] is False
        assert response.headers["x-request-id"]
        assert error["request_id"] == response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_provider_defaults_match_actual_turn_model(tmp_path) -> None:
    """Regression for the two-path drift reported by Dounai."""
    import httpx

    from tests.conftest import make_transport
    from tests.test_custom_providers import CustomBackend, capabilities, create_provider

    config = base_config(tmp_path)
    config.gateway.default_model = "alpha:one"

    def handler(request: httpx.Request) -> httpx.Response:
        return CustomBackend()(request)

    async with app_client(config, transport=make_transport(handler)) as (client, _):
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
                "event_id": "acacacac-2222-4222-8222-acacacacacac",
                "parts": [{"modality": "text", "content": "hello"}],
            },
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200, response.text
        meta_lines = [
            line for line in response.text.splitlines() if line.startswith("data: {")
        ]
        meta = next(
            __import__("json").loads(line[6:]) for line in meta_lines if '"planned_model"' in line
        )
        expected = f"custom:{stated['providerId']}:{stated['model']}"
        assert meta["planned_model"] == expected
        assert meta["requested_model"] == expected

    assert capabilities("text")["contextWindow"] == 8192
