"""Frozen downstream event contract, SSE semantics and idempotency."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import Recorder, app_client, base_config, make_transport, sse_response


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            events.append((event, json.loads("\n".join(data))))
    return events


def handler(recorder: Recorder):
    def _handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "alpha.test":
            return sse_response("streamed answer in parts")
        return sse_response("fallback")

    return _handler


@pytest.mark.asyncio
async def test_sse_event_contract(tmp_path) -> None:
    config = base_config(tmp_path)
    recorder = Recorder()
    async with app_client(config, transport=make_transport(handler(recorder))) as (client, _):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "abababab-abab-4aba-8aba-abababababab",
                "parts": [{"modality": "text", "content": "stream this"}],
            },
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-event-id"]

        events = parse_sse(response.text)
        names = [name for name, _ in events]
        assert names[0] == "meta"
        assert "route" in names
        assert "delta" in names
        assert names[-1] == "final"

        meta = next(data for name, data in events if name == "meta")
        final = next(data for name, data in events if name == "final")
        assert final["session_id"] == meta["session_id"]
        assert final["stream"] is False
        assert final["content"] == "streamed answer in parts"
        assert set(final["tags"]) == {"emotion", "type", "intent"}
        assert all(data["content"] for name, data in events if name == "delta")
        assert all(data["stream"] is True for name, data in events if name == "delta")
        assert {data["event_id"] for name, data in events if name in {"delta", "final"}} == {
            final["event_id"]
        }


@pytest.mark.asyncio
async def test_json_event_has_exact_frozen_shape(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config, transport=make_transport(handler(Recorder()))) as (client, _):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "cdcdcdcd-cdcd-4cdc-8cdc-cdcdcdcdcdcd",
                "parts": [{"modality": "text", "content": "json please"}],
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"event_id", "session_id", "content", "stream", "tags"}
        assert set(payload["tags"]) == {"emotion", "type", "intent"}
        assert payload["stream"] is False


@pytest.mark.asyncio
async def test_idempotent_event_delivery(tmp_path) -> None:
    config = base_config(tmp_path)
    recorder = Recorder()
    payload = {
        "event_id": "efefefef-efef-4efe-8efe-efefefefefef",
        "parts": [{"modality": "text", "content": "only once"}],
    }
    async with app_client(config, transport=make_transport(handler(recorder))) as (client, _):
        first = await client.post("/v1/events", json=payload)
        second = await client.post("/v1/events", json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json()["event_id"] == second.json()["event_id"]
        assert first.json()["content"] == second.json()["content"]
        assert len(recorder.with_host("alpha.test")) == 1
        detail = await client.get(f"/v1/sessions/{first.json()['session_id']}")
        assert len(detail.json()["messages"]) == 2


@pytest.mark.asyncio
async def test_input_event_compatibility_forms(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config, transport=make_transport(handler(Recorder()))) as (client, _):
        forms = [
            {
                "event_id": "10101010-1010-4010-8010-101010101010",
                "modality": "text",
                "content": "single pair",
            },
            {
                "event_id": "20202020-2020-4020-8020-202020202020",
                "content": [{"modality": "text", "content": "content list"}],
            },
            {
                "event_id": "30303030-3030-4030-8030-303030303030",
                "parts": [{"modality": "text", "content": "parts form"}],
            },
            {
                "event_id": "40404040-4040-4040-8040-404040404040",
                "content": {"modality": "text", "content": "nested form"},
            },
        ]
        for payload in forms:
            response = await client.post("/v1/events", json=payload)
            assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_invalid_event_does_not_echo_content(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config, transport=make_transport(handler(Recorder()))) as (client, _):
        response = await client.post(
            "/v1/events",
            json={"event_id": "not-a-uuid", "parts": []},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INPUT"
        assert "not-a-uuid" not in response.text


@pytest.mark.asyncio
async def test_unknown_model_is_404(tmp_path) -> None:
    config = base_config(tmp_path)
    async with app_client(config, transport=make_transport(handler(Recorder()))) as (client, _):
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "50505050-5050-4050-8050-505050505050",
                "parts": [{"modality": "text", "content": "hi"}],
                "metadata": {"model": "nope:nope"},
            },
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_MODEL"
