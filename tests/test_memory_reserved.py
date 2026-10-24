"""Reserved memory extension: disabled by default, mock-verified, never blocking.

v1.1: the external pipeline is not part of this release. These tests prove the
interface remains callable by a future extension and that disabled/unavailable
states have zero impact on the formal archive + recent-window path.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omninexus.config import MemoryConfig
from omninexus.memory.contract import build_query
from omninexus.memory.plugin import (
    DisabledMemoryPlugin,
    HttpMemoryPlugin,
    build_memory_plugin,
)
from tests.conftest import Recorder, app_client, base_config, make_transport, sse_response


def memory_config(**overrides) -> MemoryConfig:
    values = {
        "enabled": True,
        "base_url": "http://memory.test",
        "health_interval_s": 0,
        "health_timeout_ms": 200,
        "query_timeout_ms": 300,
        "write_timeout_ms": 300,
        "circuit_failures": 2,
        "circuit_cooldown_s": 5,
        "top_k": 5,
    }
    values.update(overrides)
    return MemoryConfig(**values)


def memory_ok(query: str = "用户喜欢绿茶") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "contract_version": "1.0",
            "items": [
                {
                    "id": "m1",
                    "content": query,
                    "score": 0.92,
                    "source": "archive",
                }
            ],
            "tokens": 18,
        },
    )


def health_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "ok", "contract_version": "1.0", "capabilities": ["query", "write"]},
    )


@pytest.mark.asyncio
async def test_disabled_extension_has_zero_impact_and_probe_is_unavailable(tmp_path) -> None:
    config = base_config(tmp_path)
    config.memory = MemoryConfig(enabled=False, base_url="http://memory.test")
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "alpha.test":
            return sse_response("normal answer")
        return httpx.Response(500)

    async with app_client(config, transport=make_transport(handler)) as (client, app):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "parts": [{"modality": "text", "content": "hello"}],
            },
        )
        assert response.status_code == 200
        assert not recorder.with_host("memory.test")

        status = await client.get("/v1/memory/status")
        assert status.status_code == 200
        memory = status.json()["memory"]
        assert memory["state"] == "unavailable"
        assert memory["configured"] is False
        assert memory["reserved"] is True
        assert memory["contract"]["status"] == "Reserved for future use"

        health = await client.get("/v1/health")
        assert health.json()["memory"]["state"] == "unavailable"
        assert health.json()["archive"]["formal_memory_path"] is True
        await app.state.orchestrator.drain_background()


@pytest.mark.asyncio
async def test_enabled_mock_query_injects_context_and_writeback(tmp_path) -> None:
    config = base_config(tmp_path)
    config.memory = memory_config()
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "memory.test":
            if request.url.path == "/health":
                return health_ok()
            if request.url.path == "/v1/memory/query":
                return memory_ok("用户喜欢无糖绿茶，且经常在晚上跑步")
            if request.url.path == "/v1/memory/write":
                return httpx.Response(202, json={"ok": True})
        if request.url.host == "alpha.test":
            return sse_response("好的，我记住了。")
        return httpx.Response(404)

    async with app_client(config, transport=make_transport(handler)) as (client, app):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "parts": [{"modality": "text", "content": "我喜欢喝什么？"}],
            },
        )
        assert response.status_code == 200
        provider_request = recorder.with_host("alpha.test")[-1]
        body = json.loads(provider_request.read())
        flattened = json.dumps(body["messages"], ensure_ascii=False)
        assert "无糖绿茶" in flattened
        assert "<external_memory>" in flattened

        query_request = next(
            request
            for request in recorder.requests
            if request.url.host == "memory.test" and request.url.path == "/v1/memory/query"
        )
        query_body = json.loads(query_request.read())
        assert set(query_body) == {"query"}
        assert set(query_body["query"]) == {
            "keywords",
            "entities",
            "time_range",
            "top_k",
            "priority",
        }
        assert query_body["query"]["top_k"] == 5
        assert query_request.headers["X-OmniNexus-Memory-Contract"] == "1.0"

        await app.state.orchestrator.drain_background()
        write_request = next(
            request
            for request in recorder.requests
            if request.url.host == "memory.test" and request.url.path == "/v1/memory/write"
        )
        write_body = json.loads(write_request.read())
        assert write_body["session_id"] == response.json()["session_id"]
        assert write_body["event_id"] == response.json()["event_id"]
        assert [item["role"] for item in write_body["items"]] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_memory_query_failure_does_not_interrupt_chat(tmp_path) -> None:
    config = base_config(tmp_path)
    config.memory = memory_config()
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "memory.test":
            if request.url.path == "/health":
                return health_ok()
            return httpx.Response(500, json={"error": {"code": "MEM_INTERNAL"}})
        return sse_response("answer despite memory failure")

    async with app_client(config, transport=make_transport(handler)) as (client, app):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "parts": [{"modality": "text", "content": "continue"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["content"] == "answer despite memory failure"
        await app.state.orchestrator.drain_background()
        status = await client.get("/v1/memory/status")
        assert status.json()["memory"]["state"] in {"unavailable", "circuit_open"}


@pytest.mark.asyncio
async def test_memory_timeout_does_not_interrupt_chat(tmp_path) -> None:
    config = base_config(tmp_path)
    config.memory = memory_config(health_timeout_ms=80, query_timeout_ms=80)
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "memory.test":
            raise httpx.ReadTimeout("slow memory", request=request)
        return sse_response("answer despite memory timeout")

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "parts": [{"modality": "text", "content": "continue"}],
            },
        )
        assert response.status_code == 200
        assert "answer despite" in response.json()["content"]


def test_plugin_factory_and_health_contract() -> None:
    disabled = build_memory_plugin(MemoryConfig(enabled=False))
    assert isinstance(disabled, DisabledMemoryPlugin)

    configured = build_memory_plugin(memory_config())
    assert isinstance(configured, HttpMemoryPlugin)
    assert configured.config.contract_version == "1.0"


@pytest.mark.asyncio
async def test_http_plugin_health_and_query_can_be_called_directly() -> None:
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.path == "/health":
            return health_ok()
        return memory_ok("future extension item")

    plugin = HttpMemoryPlugin(memory_config(), transport=make_transport(handler))
    try:
        health = await plugin.health(force=True)
        assert health.state == "ok"
        result = await plugin.query("future")
        assert result.available is True
        assert result.items[0]["content"] == "future extension item"
        assert plugin.public_base_url() == "http://memory.test"
    finally:
        await plugin.close()


@pytest.mark.asyncio
async def test_circuit_breaker_stops_probing_after_failures() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    plugin = HttpMemoryPlugin(
        memory_config(health_interval_s=30, circuit_failures=2, circuit_cooldown_s=30),
        transport=make_transport(handler),
    )
    try:
        assert (await plugin.health(force=True)).state == "unavailable"
        assert (await plugin.health(force=True)).state == "circuit_open"
        assert (await plugin.health(force=True)).state == "circuit_open"
        assert calls == 2
    finally:
        await plugin.close()


def test_query_builder_shape_and_no_history_fields() -> None:
    query = build_query("请记得我上周在杭州提到的项目 Alpha", top_k=5)
    assert set(query) == {"query"}
    inner = query["query"]
    assert inner["top_k"] == 5
    assert inner["priority"] == "high"
    assert inner["time_range"] is not None
    assert any("杭州" in keyword for keyword in inner["keywords"]) or "杭州" in inner["entities"]
    serialized = json.dumps(query, ensure_ascii=False)
    assert "session_id" not in serialized
    assert "messages" not in serialized
