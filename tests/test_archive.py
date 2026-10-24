"""Conversation archive is the formal memory path in v1.1."""

from __future__ import annotations

import json

import pytest

from omninexus.core.models import ContentPart, StoredMessage
from omninexus.sessions.service import ContextBuilder
from tests.conftest import app_client, base_config


def echo_config(tmp_path):
    config = base_config(tmp_path)
    config.gateway.default_model = "echo:echo"
    config.gateway.fallback_models = []
    config.models[0].fallbacks = []
    return config


@pytest.mark.asyncio
async def test_archive_create_export_delete(tmp_path) -> None:
    config = echo_config(tmp_path)
    async with app_client(config) as (client, _):
        created = await client.post(
            "/v1/sessions",
            json={
                "title": "中文存档标题",
                "model": "echo:echo",
                "prompt_profile": "default",
            },
        )
        assert created.status_code == 201
        session_id = created.json()["session"]["id"]

        first = await client.post(
            "/v1/events",
            json={
                "event_id": "11111111-1111-4111-8111-111111111111",
                "session_id": session_id,
                "parts": [{"modality": "text", "content": "第一轮问题"}],
            },
        )
        assert first.status_code == 200
        assert first.json()["session_id"] == session_id

        second = await client.post(
            "/v1/events",
            json={
                "event_id": "22222222-2222-4222-8222-222222222222",
                "session_id": session_id,
                "modality": "text",
                "content": "第二轮问题",
            },
        )
        assert second.status_code == 200

        detail = await client.get(f"/v1/sessions/{session_id}")
        assert detail.status_code == 200
        messages = detail.json()["messages"]
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert messages[0]["content"] == "第一轮问题"
        assert messages[1]["metadata"]["tags"]["emotion"] in {
            "joy",
            "sadness",
            "anger",
            "fear",
            "surprise",
            "confusion",
            "frustration",
            "gratitude",
            "neutral",
        }

        export = await client.get(f"/v1/sessions/{session_id}/export?format=json")
        assert export.status_code == 200
        assert "filename*=UTF-8''" in export.headers["content-disposition"]
        payload = json.loads(export.text)
        assert payload["format"] == "omninexus.archive.v1"
        assert len(payload["messages"]) == 4

        markdown = await client.get(f"/v1/sessions/{session_id}/export?format=md")
        assert markdown.status_code == 200
        assert "第一轮问题" in markdown.text

        deleted = await client.delete(f"/v1/sessions/{session_id}")
        assert deleted.status_code == 204
        missing = await client.get(f"/v1/sessions/{session_id}")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_session_model_and_prompt_switch_persist(tmp_path) -> None:
    config = echo_config(tmp_path)
    async with app_client(config) as (client, _):
        created = await client.post("/v1/sessions", json={"model": "echo:echo"})
        session_id = created.json()["session"]["id"]
        switched = await client.post(
            f"/v1/sessions/{session_id}/model", json={"model": "echo:echo"}
        )
        assert switched.status_code == 200
        profile = await client.post(
            "/v1/prompts/profiles",
            json={"name": "blank-2", "system_template": "Keep replies short."},
        )
        profile_id = profile.json()["profile"]["id"]
        prompt = await client.post(
            f"/v1/sessions/{session_id}/prompt", json={"profile_id": profile_id}
        )
        assert prompt.status_code == 200
        detail = await client.get(f"/v1/sessions/{session_id}")
        assert detail.json()["session"]["prompt_profile"] == profile_id


@pytest.mark.asyncio
async def test_undefined_prompt_variable_does_not_break_chat(tmp_path) -> None:
    config = echo_config(tmp_path)
    async with app_client(config) as (client, app):
        await client.put(
            "/v1/prompts/profiles/default",
            json={"system_template": "Unknown={{ not_defined_anywhere }}"},
        )
        response = await client.post(
            "/v1/events",
            json={
                "event_id": "33333333-3333-4333-8333-333333333333",
                "parts": [{"modality": "text", "content": "still works"}],
            },
        )
        assert response.status_code == 200
        assert "Offline diagnostic echo" in response.json()["content"]


def test_context_builder_keeps_latest_and_trims(tmp_path) -> None:
    config = base_config(tmp_path)
    builder = ContextBuilder(config)
    messages = [
        StoredMessage.new(
            session_id="s",
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index} " + ("word " * 80),
            parts=[ContentPart(modality="text", content=f"message {index}")],
        )
        for index in range(20)
    ]
    built = builder.build(
        system_prompt="system",
        history=messages,
        budget_tokens=600,
    )
    assert built.messages[0].role == "system"
    assert built.messages[-1].content.startswith("message 19")
    assert built.dropped_messages > 0
    assert built.estimated_tokens > 0


def test_context_builder_handles_only_system(tmp_path) -> None:
    config = base_config(tmp_path)
    built = ContextBuilder(config).build(system_prompt="only system", history=[], budget_tokens=100)
    assert built.messages[0].content == "only system"
    assert built.dropped_messages == 0


def test_context_builder_truncates_oversized_latest_turn(tmp_path) -> None:
    config = base_config(tmp_path)
    latest = StoredMessage.new(
        session_id="s",
        role="user",
        content="长文本 " * 4000,
        parts=[ContentPart(modality="text", content="长文本 " * 4000)],
    )
    built = ContextBuilder(config).build(
        system_prompt="system", history=[latest], budget_tokens=1200
    )
    assert built.truncated_latest is True
    assert len(built.messages[-1].content) < len(latest.content)
    assert built.messages[-1].content.endswith("…")
