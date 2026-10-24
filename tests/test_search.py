"""Web search is RAG with citations and strict query-only egress."""

from __future__ import annotations

import json

import httpx
import pytest

from omninexus.config import CustomSearchConfig, SearchConfig
from tests.conftest import Recorder, app_client, base_config, make_transport, sse_response


def search_config() -> SearchConfig:
    return SearchConfig(
        enabled=True,
        provider="custom",
        top_k=3,
        timeout_s=2,
        custom=CustomSearchConfig(endpoint="http://search.test/search"),
    )


@pytest.mark.asyncio
async def test_search_only_sends_current_query_and_cites_sources(tmp_path) -> None:
    config = base_config(tmp_path)
    config.search = search_config()
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "search.test":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Example source",
                            "url": "https://example.com/reference",
                            "snippet": "A useful reference snippet.",
                        }
                    ]
                },
            )
        return sse_response("Answer with citation [1].")

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        first = await client.post(
            "/v1/events",
            json={
                "event_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "parts": [{"modality": "text", "content": "private first turn"}],
            },
        )
        assert first.status_code == 200
        session_id = first.json()["session_id"]

        second = await client.post(
            "/v1/events",
            json={
                "event_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                "session_id": session_id,
                "parts": [{"modality": "text", "content": "current search question"}],
                "metadata": {"web_search": True},
            },
        )
        assert second.status_code == 200
        payload = second.json()
        assert payload["tags"]["intent"] == "tool"
        assert payload["tags"]["type"] == "text"
        assert "参考来源" in payload["content"]
        assert "https://example.com/reference" in payload["content"]

        search_request = recorder.with_host("search.test")[0]
        search_body = json.loads(search_request.read())
        assert search_body == {"query": "current search question", "top_k": 3}
        assert "private first turn" not in search_request.read().decode()

        provider_body = json.loads(recorder.with_host("alpha.test")[-1].read())
        system = "\n".join(
            message["content"]
            for message in provider_body["messages"]
            if message["role"] == "system"
        )
        assert "untrusted external data" in system
        assert "https://example.com/reference" in system

        citations = await client.get(f"/v1/events/{payload['event_id']}/citations")
        assert citations.status_code == 200
        assert citations.json()["citations"][0]["url"] == "https://example.com/reference"


@pytest.mark.asyncio
async def test_search_failure_is_non_fatal(tmp_path) -> None:
    config = base_config(tmp_path)
    config.search = search_config()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "search.test":
            return httpx.Response(503)
        return sse_response("answer without search")

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "12121212-1212-4121-8121-121212121212",
                "parts": [{"modality": "text", "content": "search fails"}],
                "metadata": {"web_search": True},
            },
        )
        assert response.status_code == 200
        assert response.json()["content"] == "answer without search"
        citations = await client.get(f"/v1/events/{response.json()['event_id']}/citations")
        assert citations.json()["citations"] == []


@pytest.mark.asyncio
async def test_search_disabled_by_default(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config) as (client, _):
        status = await client.get("/v1/search/status")
        assert status.status_code == 200
        assert status.json()["search"]["enabled"] is False
        assert status.json()["search"]["available"] is False
