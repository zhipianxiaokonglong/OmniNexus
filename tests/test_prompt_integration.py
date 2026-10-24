"""Prompt rendering is model-independent and reaches the provider system message."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import Recorder, app_client, base_config, make_transport, sse_response


@pytest.mark.asyncio
async def test_prompt_variables_are_injected_into_system_message(tmp_path) -> None:
    config = base_config(tmp_path)
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return sse_response("ok")

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        updated = await client.put(
            "/v1/prompts/profiles/default",
            json={
                "system_template": "LOCALE={{ locale }}; MODEL={{ model }}; INPUT={{ user_input }}",
                "change_note": "integration",
            },
        )
        assert updated.status_code == 200

        response = await client.post(
            "/v1/events",
            json={
                "event_id": "13131313-1313-4131-8131-131313131313",
                "parts": [{"modality": "text", "content": "hello prompt"}],
                "metadata": {"locale": "zh-CN", "model": "alpha:one"},
            },
        )
        assert response.status_code == 200

    body = json.loads(recorder.with_host("alpha.test")[-1].read())
    system = next(message["content"] for message in body["messages"] if message["role"] == "system")
    assert "LOCALE=zh-CN" in system
    assert "MODEL=alpha:one" in system
    assert "INPUT=hello prompt" in system


@pytest.mark.asyncio
async def test_prompt_is_decoupled_from_model_switch(tmp_path) -> None:
    config = base_config(tmp_path)
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return sse_response("ok")

    async with app_client(config, transport=make_transport(handler)) as (client, _):
        await client.put(
            "/v1/prompts/profiles/default",
            json={"system_template": "STABLE_PROMPT"},
        )
        first = await client.post(
            "/v1/events",
            json={
                "event_id": "14141414-1414-4141-8141-141414141414",
                "parts": [{"modality": "text", "content": "first"}],
            },
        )
        session_id = first.json()["session_id"]
        await client.post(f"/v1/sessions/{session_id}/model", json={"model": "beta:one"})
        await client.post(
            "/v1/events",
            json={
                "event_id": "15151515-1515-4151-8151-151515151515",
                "session_id": session_id,
                "parts": [{"modality": "text", "content": "second"}],
            },
        )

    for host in ("alpha.test", "beta.test"):
        body = json.loads(recorder.with_host(host)[-1].read())
        system = next(
            message["content"] for message in body["messages"] if message["role"] == "system"
        )
        assert "STABLE_PROMPT" in system
